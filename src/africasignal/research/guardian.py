"""Research Guardian observations. Disabled, unavailable and recovered are different states."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from africasignal import backup_alerts
from africasignal.backup_alerts import Finding
from africasignal.jobs.handlers import get_handler
from africasignal.models import CorrectionAction, ResearchQuestion, Setting, Source
from africasignal.research.policy import JOB_KINDS, read_policy, readiness
from africasignal.sources.permissions import current_permission

CHECKS = {
    "autonomy_configuration": "Check schema, authority, handlers and permitted source scope.",
    "research_schedule": "Check scheduled progress; paused workloads are not scheduled.",
    "research_progress": "Check durable questions and source attempts for overdue work.",
    "correction_verification": (
        "Check applied corrections for independently observed public outcomes."
    ),
}


def evaluate(session: Session, now: datetime) -> list[Finding]:
    findings: list[Finding] = []
    status, reason = readiness(session, now)
    if status == "not_scheduled":
        return findings
    if status == "blocked":
        return [
            Finding(
                "autonomy_configuration",
                "Research is blocked by configuration.",
                {"reason": reason},
            )
        ]
    policy = read_policy(session)
    invalid_sources = []
    for source_id in policy.source_ids:
        source = session.get(Source, source_id)
        permission = current_permission(session, source_id)
        if (
            source is None
            or not source.active
            or permission is None
            or not permission.may_collect
            or permission.approved_at is None
            or (permission.review_due_at is not None and permission.review_due_at <= now)
            or (
                policy.application_enabled
                and (not permission.may_republish_numbers or source.adapter not in {"nbs", "nerc"})
            )
        ):
            invalid_sources.append(source_id)
    if invalid_sources:
        return [
            Finding(
                "autonomy_configuration",
                "A configured research source no longer has the required permission.",
                {"source_ids": invalid_sources},
            )
        ]
    missing = [kind for kind in JOB_KINDS if get_handler(kind) is None]
    tables = set(inspect(session.get_bind()).get_table_names())
    required = {"research_question", "research_attempt", "research_decision", "correction_action"}
    if missing or not required <= tables:
        findings.append(
            Finding(
                "autonomy_configuration",
                "Research handlers or schema are unavailable.",
                {"missing_handlers": missing, "missing_tables": sorted(required - tables)},
            )
        )
        return findings
    if status == "waiting_window":
        return findings
    row = session.get(Setting, "research.planner_progress")
    moment = None
    if row and isinstance(row.value, dict):
        try:
            moment = datetime.fromisoformat(row.value["at"])
        except (KeyError, ValueError, TypeError):
            pass
    if moment is None or moment.tzinfo is None or now - moment > timedelta(minutes=15):
        findings.append(
            Finding("research_schedule", "Research planner has no recent completed pass.", {})
        )
    stale = list(
        session.scalars(
            select(ResearchQuestion)
            .where(
                ResearchQuestion.state.in_(("queued", "researching", "evidence_ready", "applying")),
                ResearchQuestion.updated_at < now - timedelta(minutes=policy.progress_minutes),
            )
            .limit(20)
        )
    )
    if stale:
        findings.append(
            Finding(
                "research_progress",
                "Research questions have stopped making progress.",
                {"question_ids": [q.id for q in stale], "count": len(stale)},
            )
        )
    overdue = list(
        session.scalars(
            select(CorrectionAction)
            .where(
                CorrectionAction.state == "verifying",
                CorrectionAction.created_at < now - timedelta(minutes=15),
            )
            .limit(20)
        )
    )
    if overdue:
        findings.append(
            Finding(
                "correction_verification",
                "Applied corrections have not received a public verification result.",
                {"question_ids": [a.question_id for a in overdue], "count": len(overdue)},
            )
        )
    return findings


def _debounced(session: Session, now: datetime, findings: list[Finding]) -> list[Finding]:
    """Open after two bad checks and recover after two good checks."""
    current = {f.code: f for f in findings}
    result: list[Finding] = []
    for code in CHECKS:
        key = f"research.guardian.{code}"
        row = session.get(Setting, key)
        state = dict(row.value) if row and isinstance(row.value, dict) else {}
        failure = current.get(code)
        if failure:
            state["failures"] = int(state.get("failures", 0)) + 1
            state["successes"] = 0
            state["finding"] = {"summary": failure.summary, "detail": failure.detail}
            if state["failures"] >= 2:
                result.append(failure)
        elif state.get("failures", 0):
            state["successes"] = int(state.get("successes", 0)) + 1
            if state["successes"] < 2 and isinstance(state.get("finding"), dict):
                old = state["finding"]
                result.append(Finding(code, str(old["summary"]), dict(old["detail"])))
            else:
                state = {"failures": 0, "successes": 0}
        session.merge(Setting(key=key, value=state))
    return result


def check(session: Session, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    findings = evaluate(session, now)
    backup_alerts.apply(session, now, tuple(CHECKS), _debounced(session, now, findings))


def status(session: Session) -> dict[str, object]:
    state, reason = readiness(session)
    policy = read_policy(session)
    return {"state": state, "reason": reason, "policy": policy.model_dump(), "checks": CHECKS}
