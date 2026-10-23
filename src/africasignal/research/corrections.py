"""Scoped application and actual public outcome checks, independent of job return status."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from africasignal import settings_store
from africasignal.config import get_settings
from africasignal.evidence.capture import capture, clear_processing_content
from africasignal.guardian.public_pages import _public_origin, _read_page
from africasignal.jobs.handlers import JobContext
from africasignal.models import (
    CorrectionAction,
    EvidenceDocument,
    GuardianCheckRun,
    ResearchDecision,
    ResearchQuestion,
    Situation,
    Source,
)
from africasignal.net.request_scope import ScopeRefused, request_scope
from africasignal.operations.delivery_corrections import record_for_version
from africasignal.publish.factfmt import fact_value_text
from africasignal.publish.invalidation import plan_corrections
from africasignal.publish.situations import assess_situation
from africasignal.publish.versions import apply_policy, cancel_pending_delivery
from africasignal.research.executor import current, transition
from africasignal.research.judge import passage
from africasignal.research.planner import mark
from africasignal.research.policy import read_policy
from africasignal.research.processing_scope import processing_scope
from africasignal.research.sources import permitted, scope_for
from africasignal.sources.base import AdapterContext, get_adapter
from africasignal.storage import get_store
from africasignal.web.queries import current_situation


def apply(ctx: JobContext) -> None:
    session = ctx.session
    question = current(session, ctx.job.payload, {"validated"})
    if question is None:
        return
    policy = read_policy(session)
    decision = session.scalar(
        select(ResearchDecision).where(ResearchDecision.question_id == question.id)
    )
    if decision is None or decision.verdict != "supported":
        transition(session, question, "escalated", "missing_validated_decision")
        return
    if policy.mode != "canary" or not policy.application_enabled:
        transition(session, question, "unresolved", "application_disabled")
        return
    from africasignal.research.qualification import accepted_release

    if not accepted_release(session, policy.qualification_ref):
        transition(session, question, "escalated", "qualification_unavailable")
        return
    doc = session.get(EvidenceDocument, decision.evidence_id)
    if doc is None or doc.content_sha256 != decision.provenance.get("content_sha256"):
        transition(session, question, "escalated", "evidence_changed")
        return
    source = session.get(Source, doc.source_id)
    if source is None or source.adapter not in {"nbs", "nerc"}:
        transition(session, question, "escalated", "adapter_requires_review")
        return
    try:
        passage(session, doc, datetime.now(UTC))
    except ScopeRefused:
        transition(session, question, "escalated", "evidence_no_longer_eligible")
        return
    existing = session.scalar(
        select(CorrectionAction).where(CorrectionAction.question_id == question.id)
    )
    if existing is not None:
        transition(session, question, "verifying", "prior_action", "verify_research")
        return
    action = CorrectionAction(
        question_id=question.id,
        decision_id=decision.id,
        before_version_id=question.version_id,
        state="queued",
    )
    session.add(action)
    transition(session, question, "applying", "scoped_adapter_processing")
    session.commit()
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    fresh: EvidenceDocument | None = None
    try:
        store = get_store()
        # Re-fetch when transient bytes were not retained. Never parse the retained quote as a PDF.
        with request_scope(scope_for(factory, question.id, source.id, ctx.job.id)):
            session.scalar(select(Source).where(Source.id == source.id).with_for_update(read=True))
            fresh = capture(
                session, store, source, doc.url, title=doc.title, published_at=doc.published_at
            )
        if fresh.content_sha256 != decision.provenance.get("content_sha256"):
            clear_processing_content(fresh)
            transition(session, question, "escalated", "source_changed_after_decision")
            action.state, action.reason = "blocked", "source_changed_after_decision"
            return
        # Pin policy/current version and collection/reuse permission through the factual commit.
        question = current(session, ctx.job.payload, {"applying"})
        if question is None:
            clear_processing_content(fresh)
            return
        session.scalar(select(Source).where(Source.id == source.id).with_for_update(read=True))
        permitted(session, source, datetime.now(UTC), reuse=True)
        adapter = get_adapter(source.adapter)
        assert adapter is not None
        with processing_scope(question.scope):
            result = adapter.process(fresh, AdapterContext(session=session, store=store))
        clear_processing_content(fresh)
        correction = plan_corrections(session, "measurement", result.superseded).get(
            question.situation_id
        )
        now = datetime.now(UTC)
        outcome = assess_situation(session, question.situation_id, now, correction=correction)
        if outcome.outcome == "created" and outcome.version is not None:
            mark(
                session,
                f"research.no_external.version.{outcome.version.id}",
                {"question_id": question.id},
            )
            apply_policy(
                session, outcome.version.id, now, kind="correction" if correction else "new_version"
            )
        situation = session.get(Situation, question.situation_id)
        assert situation is not None
        if situation.current_version_id == question.version_id:
            action.state, action.reason = "blocked", "no_eligible_replacement"
            transition(session, question, "unresolved", "no_eligible_replacement")
            return
        action.after_version_id = situation.current_version_id
        action.state, action.reason = "verifying", "awaiting_public_observation"
        record_for_version(
            session,
            question.version_id,
            reason_key=f"research:{question.id}",
            reason_code="research_correction",
            reason_detail=correction or "A newly verified source changed the current assessment.",
            now=now,
        )
        cancel_pending_delivery(session, question.version_id, now)
        transition(session, question, "verifying", "awaiting_public_observation", "verify_research")
    except Exception:
        if fresh is not None:
            clear_processing_content(fresh)
        session.rollback()
        question = session.get(ResearchQuestion, int(ctx.job.payload["question_id"]))
        failed_action = session.scalar(
            select(CorrectionAction).where(
                CorrectionAction.question_id == int(ctx.job.payload["question_id"])
            )
        )
        if question:
            transition(session, question, "escalated", "application_failed")
        if failed_action is not None:
            failed_action.state, failed_action.reason = "blocked", "application_failed"


def observe(
    session: Session,
    question: ResearchQuestion,
    action: CorrectionAction,
    client: httpx.Client,
    origin: str,
) -> tuple[bool, dict[str, object]]:
    situation = session.get(Situation, question.situation_id)
    if situation is None:
        return False, {"reason": "situation_missing"}
    current_view = current_situation(session, situation.slug)
    if current_view is None or current_view.version.id != action.after_version_id:
        return False, {"reason": "replacement_no_longer_public"}
    route = f"/s/{quote(situation.slug, safe='')}"
    _, page, _ = _read_page(client, origin, route)
    _, history, _ = _read_page(client, origin, route + "/history")
    expected = [
        [
            " ".join(str(f.get("label") or "").split()),
            " ".join(fact_value_text(f).split()),
            " ".join(str(f.get("period") or "").split()),
            " ".join(str(f.get("source_label") or "").split()),
        ]
        for f in current_view.version.facts
    ]
    good = (
        page.get("guardian_version_id") == str(action.after_version_id)
        and page.get("guardian_status") == current_view.effective_status
        and page.get("guardian_fact_rows") == expected
        and history.get("guardian_current_version_id") == str(action.after_version_id)
    )
    return good, {
        "version_id": page.get("guardian_version_id"),
        "facts_match": good,
        "history_version_id": history.get("guardian_current_version_id"),
    }


def verify(ctx: JobContext) -> None:
    session = ctx.session
    # A corrected situation legitimately has a newer current version than the question's input.
    question = session.scalar(
        select(ResearchQuestion)
        .where(ResearchQuestion.id == int(ctx.job.payload["question_id"]))
        .with_for_update()
    )
    if question is None or question.generation != ctx.job.payload.get("generation"):
        return
    action = session.scalar(
        select(CorrectionAction).where(CorrectionAction.question_id == question.id)
    )
    if action is None or action.state not in {"verifying", "queued"}:
        return
    now = datetime.now(UTC)
    raw = get_settings().guardian_public_base_url.strip() or settings_store.get(
        session, "public_base_url"
    )
    try:
        origin = _public_origin(raw or "")
        with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
            passed, observed = observe(session, question, action, client, origin)
        state = "pass" if passed else "fail"
    except (ValueError, httpx.HTTPError):
        passed, state, observed = False, "unknown", {"reason": "public_observation_unavailable"}
    session.add(
        GuardianCheckRun(
            check_code="public_correction_consistency",
            subject_key=str(question.id),
            status=state,
            summary="Public correction outcome",
            observed=observed,
            checked_at=now,
        )
    )
    action.checked_at = now
    if passed:
        action.state, action.reason = "verified", "public_facts_and_history_match"
        transition(session, question, "resolved", "public_correction_verified")
    elif now >= question.deadline_at:
        action.state, action.reason = "blocked", "public_verification_deadline"
        transition(session, question, "escalated", "public_verification_deadline")
    else:
        transition(session, question, "verifying", "public_verification_pending")
