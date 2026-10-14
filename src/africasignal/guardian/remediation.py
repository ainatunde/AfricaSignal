"""One bounded, permission-checked retry for transient source collection failures."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.jobs import queue
from africasignal.models import GuardianRepairAttempt, Job, Source
from africasignal.sources.permissions import current_permission

RETRY_DELAY = timedelta(minutes=15)
REPAIR_WINDOW = timedelta(hours=24)
RECHECK_LIMIT = 100
HTTP_FAILURE = re.compile(r"\bHTTP (408|500|502|503|504)\b", re.IGNORECASE)


def transient_fetch_error(last_error: str | None) -> str | None:
    """Return a stable reason code only for recognized, replay-safe fetch failures."""
    if not last_error:
        return None
    lowered = last_error.casefold()
    if "request timed out:" in lowered:
        return "network_timeout"
    if "connection refused/failed:" in lowered:
        return "connection_failure"
    if "http transport error:" in lowered:
        return "transport_error"
    match = HTTP_FAILURE.search(last_error)
    if match is not None:
        return f"http_{match.group(1)}"
    return None


def _incident_key(source: Source, generation_at: datetime) -> str:
    material = f"{source.id}:{generation_at.astimezone(UTC).isoformat()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _recheck_attempts(session: Session, now: datetime) -> None:
    attempts = list(
        session.scalars(
            select(GuardianRepairAttempt)
            .where(GuardianRepairAttempt.state == "queued")
            .order_by(GuardianRepairAttempt.created_at)
            .limit(RECHECK_LIMIT)
            .with_for_update(skip_locked=True)
        )
    )
    for attempt in attempts:
        if attempt.retry_job_id is None:
            continue
        retry = session.get(Job, attempt.retry_job_id)
        if retry is None:
            source = session.get(Source, attempt.source_id)
            recovered = (
                source is not None
                and source.last_success_at is not None
                and source.last_success_at > attempt.source_generation_at
            )
            attempt.state = "recovered" if recovered else "completed_without_recovery"
            attempt.checked_at = now
            attempt.completed_at = now
            continue  # successful jobs are archived after 90 days; dead jobs are retained
        if retry.status in ("queued", "running", "failed"):
            attempt.checked_at = now
            continue
        source = session.get(Source, attempt.source_id)
        recovered = (
            retry.status == "done"
            and source is not None
            and source.last_success_at is not None
            and source.last_success_at > attempt.source_generation_at
        )
        attempt.state = (
            "recovered"
            if recovered
            else ("exhausted" if retry.status == "dead" else "completed_without_recovery")
        )
        attempt.checked_at = now
        attempt.completed_at = now
        audit.record_system(
            session,
            "guardian.repair.rechecked",
            "guardian_repair_attempt",
            attempt.id,
            after={"state": attempt.state, "error_class": attempt.error_class},
        )


def check_and_repair(session: Session, now: datetime | None = None) -> int:
    """Reconcile finished repairs and schedule at most one transient retry per source generation.

    The normal queue already retries a fetch five times. This additional action is limited to one
    delayed replay-safe GET after a recognized transport/server failure, then waits for an outcome
    check. Parser, permission, policy and delivery failures are never repaired here.
    """
    now = now or datetime.now(UTC)
    _recheck_attempts(session, now)
    dead_jobs = session.execute(
        select(Job.id, Job.payload, Job.last_error, Job.finished_at)
        .where(
            Job.kind == "fetch_source",
            Job.status == "dead",
            Job.finished_at >= now - REPAIR_WINDOW,
        )
        .order_by(Job.finished_at, Job.id)
        .limit(RECHECK_LIMIT)
    ).all()
    scheduled = 0
    for job_id, payload, last_error, finished_at in dead_jobs:
        error_class = transient_fetch_error(last_error)
        source_id = payload.get("source_id") if isinstance(payload, dict) else None
        if error_class is None or not isinstance(source_id, int):
            continue
        source = session.scalar(select(Source).where(Source.id == source_id).with_for_update())
        if source is None or not source.active or source.health != "failing":
            continue
        permission = current_permission(session, source.id)
        if permission is None or not permission.may_collect:
            continue
        generation_at = source.last_success_at or source.created_at
        if finished_at is None or generation_at > finished_at:
            continue  # a later successful fetch already recovered this source

        incident_key = _incident_key(source, generation_at)
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"guardian.fetch_source.{incident_key}"},
        )
        existing = session.scalar(
            select(GuardianRepairAttempt.id).where(
                GuardianRepairAttempt.incident_key == incident_key
            )
        )
        if existing is not None:
            continue

        dedupe_key = f"guardian.fetch_source.{incident_key}"
        retry_id = queue.enqueue(
            session,
            "fetch_source",
            {"source_id": source.id},
            dedupe_key=dedupe_key,
            run_at=now + RETRY_DELAY,
            max_attempts=1,
        )
        if retry_id is None:
            retry_id = session.scalar(select(Job.id).where(Job.dedupe_key == dedupe_key))
        if retry_id is None:
            continue
        attempt = GuardianRepairAttempt(
            incident_key=incident_key,
            source_id=source.id,
            failed_job_id=job_id,
            retry_job_id=retry_id,
            source_generation_at=generation_at,
            error_class=error_class,
            state="queued",
            checked_at=now,
        )
        session.add(attempt)
        session.flush()
        audit.record_system(
            session,
            "guardian.repair.queued",
            "guardian_repair_attempt",
            attempt.id,
            after={
                "source_id": source.id,
                "failed_job_id": job_id,
                "retry_job_id": retry_id,
                "error_class": error_class,
                "delay_seconds": int(RETRY_DELAY.total_seconds()),
            },
        )
        scheduled += 1
    return scheduled
