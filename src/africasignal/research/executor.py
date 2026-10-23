"""Bounded research jobs. All factual application uses existing source adapters and policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from africasignal import audit
from africasignal.evidence.capture import capture, clear_processing_content
from africasignal.evidence.origins import assign_origin
from africasignal.jobs.handlers import JobContext
from africasignal.llm import BudgetExhausted, LlmNotConfigured, WorkloadUnavailable, build_adapter
from africasignal.models import (
    EvidenceDocument,
    ResearchAttempt,
    ResearchDecision,
    ResearchQuestion,
    Situation,
    Source,
)
from africasignal.net.request_scope import ScopeRefused, request_scope
from africasignal.research.judge import PROMPT, judge, passage
from africasignal.research.planner import enqueue_step
from africasignal.research.policy import read_policy, readiness
from africasignal.research.sources import permitted, scope_for
from africasignal.sources.base import AdapterContext, get_adapter
from africasignal.storage import get_store


def current(
    session: Session, payload: dict[str, Any], allowed: set[str]
) -> ResearchQuestion | None:
    now = datetime.now(UTC)
    question = session.scalar(
        select(ResearchQuestion)
        .where(ResearchQuestion.id == int(payload["question_id"]))
        .with_for_update()
    )
    if question is None or question.generation != payload.get("generation"):
        return None
    policy = read_policy(session, lock=True)
    situation = session.get(Situation, question.situation_id)
    if question.state not in allowed:
        return None
    if (
        question.policy_revision != policy.revision
        or situation is None
        or situation.current_version_id != question.version_id
    ):
        question.state, question.reason, question.updated_at = (
            "cancelled",
            "generation_changed",
            now,
        )
        return None
    state, reason = readiness(session, now)
    if state != "ready":
        question.state = "cancelled" if reason == "policy_off" else "waiting_window"
        question.reason, question.updated_at = reason, now
        question.next_due_at = now + timedelta(minutes=5)
        return None
    if question.deadline_at <= now:
        question.state, question.reason = "escalated", "deadline"
        return None
    return question


def transition(
    session: Session,
    question: ResearchQuestion,
    state: str,
    reason: str,
    next_job: str | None = None,
) -> None:
    now = datetime.now(UTC)
    question.state, question.reason, question.updated_at = state, reason, now
    question.next_due_at = now + timedelta(minutes=5)
    audit.record_system(
        session,
        "research.transition",
        "research_question",
        question.id,
        after={"state": state, "reason": reason},
    )
    session.flush()
    if next_job:
        enqueue_step(session, question, next_job, now)


def research(ctx: JobContext) -> None:
    session = ctx.session
    question = current(session, ctx.job.payload, {"queued"})
    if question is None:
        return
    policy = read_policy(session)
    # The planner durably limits the hourly number of newly queued generations. A worker
    # recheck here would count repeated state transitions as new work and could exhaust quota.
    transition(session, question, "researching", "approved_source_discovery")
    session.commit()
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    now = datetime.now(UTC)
    candidates = 0
    failures: list[str] = []
    try:
        store = get_store()
    except Exception:
        question = current(session, ctx.job.payload, {"researching"})
        if question is not None:
            transition(session, question, "waiting_window", "storage_unavailable")
            question.next_due_at = datetime.now(UTC) + timedelta(minutes=15)
        return
    for source_id in policy.source_ids:
        source = session.get(Source, source_id)
        if source is None:
            continue
        source_failed = False
        try:
            permitted(session, source, now)
            adapter = get_adapter(source.adapter)
            if adapter is None or source.adapter == "gdelt":
                continue
            # No research lock is held across remote requests or durable request reservations.
            session.commit()
            with request_scope(scope_for(factory, question.id, source_id, ctx.job.id)):
                items = adapter.discover(source, AdapterContext(session=session, store=store))
                # Also revisit previously captured URLs, so an edited document is not hidden
                # by process_document's lifetime URL deduplication key.
                old = list(
                    session.scalars(
                        select(EvidenceDocument)
                        .where(EvidenceDocument.source_id == source_id)
                        .order_by(EvidenceDocument.retrieved_at.desc())
                        .limit(policy.max_documents)
                    )
                )
                urls = [(item.url, item.title, item.published_at) for item in items]
                urls += [(d.url, d.title, d.published_at) for d in old]
                seen: set[str] = set()
                for url, title, published_at in urls:
                    if url in seen or candidates >= policy.max_documents:
                        continue
                    seen.add(url)
                    permitted(session, source, datetime.now(UTC))
                    session.commit()
                    session.scalar(
                        select(Source).where(Source.id == source_id).with_for_update(read=True)
                    )
                    doc = capture(
                        session, store, source, url, title=title, published_at=published_at
                    )
                    clear_processing_content(doc)
                    assign_origin(session, doc)
                    session.flush()
                    attempt = session.scalar(
                        select(ResearchAttempt)
                        .where(
                            ResearchAttempt.question_id == question.id,
                            ResearchAttempt.source_id == source_id,
                            ResearchAttempt.evidence_id.is_(None),
                        )
                        .order_by(ResearchAttempt.id.desc())
                        .limit(1)
                    )
                    if attempt:
                        attempt.evidence_id, attempt.outcome = doc.id, "captured"
                    candidates += 1
                    session.commit()
            for attempt in session.scalars(
                select(ResearchAttempt).where(
                    ResearchAttempt.question_id == question.id,
                    ResearchAttempt.source_id == source_id,
                    ResearchAttempt.outcome == "reserved",
                )
            ):
                attempt.outcome = "completed"
            session.commit()
        except ScopeRefused as exc:
            session.rollback()
            source_failed = True
            reason = str(exc)
            if "request_budget_exhausted" in reason:
                failures.append("research_request_budget_exhausted")
            else:
                failures.append(reason)
        except Exception:
            session.rollback()
            source_failed = True
            # Do not persist third-party response bodies or exception URLs in research logs.
            failures.append("source_fetch_or_parse_failed")
        finally:
            with factory() as attempts_session:
                for attempt in attempts_session.scalars(
                    select(ResearchAttempt).where(
                        ResearchAttempt.question_id == question.id,
                        ResearchAttempt.source_id == source_id,
                        ResearchAttempt.job_id == ctx.job.id,
                        ResearchAttempt.outcome == "reserved",
                    )
                ):
                    attempt.outcome = "failed" if source_failed else "completed"
                attempts_session.commit()
        if candidates >= policy.max_documents:
            break
    session.expire_all()
    question = current(session, ctx.job.payload, {"researching"})
    if question is None:
        return
    if candidates:
        transition(session, question, "evidence_ready", "captured_evidence", "judge_research")
    else:
        reason = failures[0] if failures else "no_candidate_document"
        if (
            reason == "research_request_budget_exhausted"
            and question.fetch_count < policy.max_attempts
        ):
            transition(session, question, "waiting_budget", "fetch_budget_exhausted")
            question.next_due_at = datetime.now(UTC) + timedelta(hours=1)
        elif (
            reason == "source_fetch_or_parse_failed" and question.fetch_count < policy.max_attempts
        ):
            transition(session, question, "waiting_window", "bounded_transport_retry")
        else:
            transition(session, question, "escalated", reason)


def decide(ctx: JobContext) -> None:
    session = ctx.session
    question = current(session, ctx.job.payload, {"evidence_ready"})
    if question is None:
        return
    policy = read_policy(session)
    if not policy.decisions_enabled:
        transition(session, question, "unresolved", "semantic_decisions_disabled")
        return
    try:
        adapter = build_adapter(session)
    except LlmNotConfigured:
        transition(session, question, "unresolved", "model_unavailable")
        return
    documents = list(
        session.scalars(
            select(EvidenceDocument)
            .join(ResearchAttempt, ResearchAttempt.evidence_id == EvidenceDocument.id)
            .where(ResearchAttempt.question_id == question.id)
            .order_by(EvidenceDocument.id.desc())
            .distinct()
            .limit(policy.max_documents)
        )
    )
    selected: tuple[EvidenceDocument, Any] | None = None
    for doc in documents:
        if question.model_count >= policy.max_model_calls:
            break
        try:
            passage(session, doc, datetime.now(UTC))
            question.model_count += 1
            session.commit()  # model attempt allowance survives provider errors and worker death
            result = judge(session, adapter, question, doc, datetime.now(UTC), ctx.job.id)
        except BudgetExhausted:
            transition(session, question, "waiting_budget", "model_budget_exhausted")
            question.model_count = max(0, question.model_count - 1)
            question.next_due_at = datetime.now(UTC) + timedelta(hours=1)
            return
        except WorkloadUnavailable as exc:
            transition(session, question, "waiting_window", "ai_workload_paused")
            question.model_count = max(0, question.model_count - 1)
            question.next_due_at = exc.retry_at
            return
        except ScopeRefused:
            continue
        except Exception:
            session.rollback()
            session.refresh(question)
            transition(session, question, "escalated", "semantic_validation_failed")
            return
        session.expire_all()
        question = current(session, ctx.job.payload, {"evidence_ready"})
        if question is None:
            return
        try:
            passage(session, doc, datetime.now(UTC))
        except ScopeRefused:
            transition(session, question, "escalated", "evidence_changed_during_judgment")
            return
        attempt = session.scalar(
            select(ResearchAttempt)
            .where(
                ResearchAttempt.question_id == question.id,
                ResearchAttempt.evidence_id == doc.id,
            )
            .order_by(ResearchAttempt.id.desc())
            .limit(1)
        )
        if attempt is not None:
            attempt.outcome = "completed"
            attempt.detail = {**attempt.detail, "verdict": result.verdict, "reason": result.reason}
        selected = (doc, result)
        if result.verdict == "supported":
            break
    if selected is None:
        transition(session, question, "unresolved", "no_current_supported_passage")
        return
    doc, result = selected
    decision = ResearchDecision(
        question_id=question.id,
        evidence_id=doc.id,
        verdict=result.verdict,
        method="model_validated",
        provenance={
            "prompt": PROMPT,
            "model": adapter.model_for("research"),
            "validator": "research_scope_v1",
            "reason": result.reason,
            "content_sha256": doc.content_sha256,
            "policy_revision": policy.revision,
        },
    )
    session.add(decision)
    session.flush()
    if result.verdict == "supported":
        transition(session, question, "validated", "evidence_supported", "apply_research")
    else:
        transition(session, question, "unresolved", result.verdict)
