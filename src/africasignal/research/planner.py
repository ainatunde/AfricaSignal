"""Bounded, cursor-based discovery of scoped gaps; no facts are created here."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from africasignal.jobs import queue
from africasignal.models import (
    AssessmentVersion,
    Place,
    ResearchAttempt,
    ResearchQuestion,
    Setting,
    Situation,
)
from africasignal.publish.eligibility import publication_eligibility
from africasignal.research.policy import ResearchPolicy, read_policy, readiness
from africasignal.sources.permissions import current_permission

TERMINAL = ("resolved", "escalated", "cancelled", "unresolved")
RETRYABLE_REASONS = {
    "no_current_supported_passage",
    "no_candidate_document",
    "source_fetch_or_parse_failed",
    "model_unavailable",
    "contradicted",
    "insufficient",
    "out_of_scope",
    "source_changed_after_decision",
    "evidence_changed_during_judgment",
}


def mark(session: Session, key: str, value: dict[str, object]) -> None:
    stmt = insert(Setting).values(key=key, value=value)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Setting.key], set_={"value": stmt.excluded.value}
        )
    )


def gap(version: AssessmentVersion, now: datetime, eligible: bool) -> str | None:
    if not eligible:
        return "ineligible_evidence"
    if version.evidence_state == "disputed":
        return "evidence_disagreement"
    if version.evidence_state == "insufficient":
        return "missing_primary_evidence"
    if version.valid_until is not None and version.valid_until <= now:
        return "stale_reference"
    # Only gaps a currently approved primary-source adapter can resolve should create work.
    # Do not research unknowable causation, billing, geographic granularity, or media disagreement.
    for unknown in version.unknowns:
        lowered = unknown.lower()
        if (
            lowered.startswith("the value for ")
            or "latest " in lowered
            and " figure is for " in lowered
            or "official documents state different rates" in lowered
            or "the rate before this one is not available" in lowered
            or "the date this rate takes effect is not stated" in lowered
        ):
            return "primary_source_gap"
    return None


def enqueue_step(session: Session, question: ResearchQuestion, kind: str, now: datetime) -> None:
    queue.enqueue(
        session,
        kind,
        {"question_id": question.id, "generation": question.generation},
        dedupe_key=f"research:{question.id}:{kind}:{question.updated_at.isoformat()}",
        run_at=max(now, question.next_due_at),
        max_attempts=1,
    )


def create_question(
    session: Session,
    situation: Situation,
    version: AssessmentVersion,
    kind: str,
    policy: ResearchPolicy,
    now: datetime,
) -> ResearchQuestion:
    prior = session.scalar(
        select(ResearchQuestion)
        .where(
            ResearchQuestion.situation_id == situation.id,
            ResearchQuestion.kind == kind,
        )
        .order_by(ResearchQuestion.updated_at.desc(), ResearchQuestion.id.desc())
        .limit(1)
    )
    retry_epoch = 0
    if (
        prior is not None
        and prior.state in {"unresolved", "escalated"}
        and prior.reason in RETRYABLE_REASONS
    ):
        age = now - prior.updated_at
        if age >= timedelta(hours=24):
            retry_epoch = int(age.total_seconds() // 86_400)
    permission_scope = []
    for source_id in policy.source_ids:
        permission = current_permission(session, source_id)
        permission_scope.append(
            None
            if permission is None
            else [
                permission.version,
                permission.may_collect,
                permission.may_store_full_text,
                permission.may_republish_numbers,
                permission.review_due_at.isoformat() if permission.review_due_at else None,
            ]
        )
    material = json.dumps(
        [
            version.id,
            version.inputs_hash,
            policy.model_dump(mode="json"),
            permission_scope,
            retry_epoch,
        ],
        sort_keys=True,
    )
    generation = hashlib.sha256(material.encode()).hexdigest()
    identity = f"{situation.id}:{kind}:{generation}"
    place = session.get(Place, situation.place_id)
    scope = {
        "topic": situation.topic,
        "item_code": situation.item_code,
        "policy_series": situation.policy_series,
        "place_id": situation.place_id,
        "place_code": place.code if place else "unknown",
        "period": version.period_label,
        "template": version.template,
    }
    stmt = insert(ResearchQuestion).values(
        identity=identity,
        situation_id=situation.id,
        version_id=version.id,
        kind=kind,
        generation=generation,
        policy_revision=policy.revision,
        scope=scope,
        state="open",
        reason="gap_detected",
        next_due_at=now,
        deadline_at=now + timedelta(hours=24),
        updated_at=now,
        fetch_count=0,
        model_count=0,
    )
    session.execute(stmt.on_conflict_do_nothing(index_elements=[ResearchQuestion.identity]))
    question = session.scalar(select(ResearchQuestion).where(ResearchQuestion.identity == identity))
    assert question is not None
    obsolete = session.scalars(
        select(ResearchQuestion).where(
            ResearchQuestion.situation_id == situation.id,
            ResearchQuestion.kind == kind,
            ResearchQuestion.id != question.id,
            ResearchQuestion.state.not_in(TERMINAL),
        )
    )
    for old in obsolete:
        old.state, old.reason, old.updated_at = "cancelled", "new_generation", now
    return question


def plan(session: Session, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('research.planner'))"))
    if readiness(session, now)[0] != "ready":
        return 0
    policy = read_policy(session, lock=True)
    cursor_row = session.get(Setting, "research.planner_cursor")
    cursor = int(cursor_row.value.get("id", 0)) if cursor_row else 0
    rows = list(
        session.execute(
            select(Situation, AssessmentVersion)
            .join(AssessmentVersion, Situation.current_version_id == AssessmentVersion.id)
            .where(Situation.id > cursor, Situation.status == "active")
            .order_by(Situation.id)
            .limit(100)
        )
    )
    for situation, version in rows:
        if situation.topic not in {"food", "energy"}:
            continue
        decision = publication_eligibility(session, version, channel="website", now=now)
        kind = gap(version, now, decision.eligible)
        if kind:
            create_question(session, situation, version, kind, policy, now)
    mark(session, "research.planner_cursor", {"id": rows[-1][0].id if len(rows) == 100 else 0})
    due = list(
        session.scalars(
            select(ResearchQuestion)
            .where(ResearchQuestion.state.not_in(TERMINAL), ResearchQuestion.next_due_at <= now)
            .order_by(ResearchQuestion.updated_at, ResearchQuestion.id)
            .limit(100)
            .with_for_update()
        )
    )
    started = (
        session.scalar(
            select(func.count(func.distinct(ResearchAttempt.question_id))).where(
                ResearchAttempt.created_at >= now - timedelta(hours=1)
            )
        )
        or 0
    )
    queued = (
        session.scalar(
            select(func.count())
            .select_from(ResearchQuestion)
            .where(ResearchQuestion.state == "queued")
        )
        or 0
    )
    admitted = min(policy.max_questions_per_hour, started + queued)
    for question in due:
        if question.deadline_at <= now:
            question.state, question.reason, question.updated_at = "escalated", "deadline", now
            continue
        if question.state == "waiting_budget" and question.reason == "model_budget_exhausted":
            question.state, question.updated_at = "evidence_ready", now
            enqueue_step(session, question, "judge_research", now)
        elif question.state == "waiting_window" and question.reason == "ai_workload_paused":
            question.state, question.updated_at = "evidence_ready", now
            enqueue_step(session, question, "judge_research", now)
        elif question.state in {"open", "waiting_permission", "waiting_budget", "waiting_window"}:
            if admitted >= policy.max_questions_per_hour:
                question.next_due_at = now + timedelta(minutes=5)
                continue
            question.state, question.updated_at = "queued", now
            admitted += 1
            enqueue_step(session, question, "research_question", now)
        elif question.state == "evidence_ready":
            enqueue_step(session, question, "judge_research", now)
        elif question.state == "validated":
            enqueue_step(session, question, "apply_research", now)
        elif question.state in {"applying", "verifying"}:
            enqueue_step(session, question, "verify_research", now)
        elif question.updated_at < now - timedelta(minutes=15):
            # Never restart an uncertain model/processing attempt automatically.
            question.state, question.reason = "escalated", "attempt_progress_unknown"
    mark(session, "research.planner_progress", {"at": now.isoformat(), "scanned": len(rows)})
    return len(due)
