"""Jobs page: queue state by status and kind, dead jobs with their error, and retry."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.models import Job, Operator

STATUSES = ("queued", "running", "failed", "dead", "done")
ERROR_LIMIT = 600


class JobError(ValueError):
    """A refusal the operator should see."""


@dataclass(frozen=True)
class KindCounts:
    kind: str
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def counts_by_kind(session: Session) -> list[KindCounts]:
    """One row per job kind with a count for every status, kinds with dead jobs first."""
    grouped: dict[str, dict[str, int]] = {}
    for kind, status, number in session.execute(
        select(Job.kind, Job.status, func.count()).group_by(Job.kind, Job.status)
    ):
        grouped.setdefault(kind, {s: 0 for s in STATUSES})[status] = number
    rows = [KindCounts(kind, counts) for kind, counts in grouped.items()]
    rows.sort(key=lambda r: (-r.counts["dead"], -r.counts["failed"], r.kind))
    return rows


def totals(session: Session) -> dict[str, int]:
    result = {s: 0 for s in STATUSES}
    for status, number in session.execute(select(Job.status, func.count()).group_by(Job.status)):
        result[status] = number
    return result


def dead_jobs(session: Session, limit: int = 100) -> list[Job]:
    """Newest first. The error is shortened for display; the payload is not shown at all."""
    return list(
        session.scalars(
            select(Job)
            .where(Job.status.in_(("dead", "failed")))
            .order_by(Job.finished_at.desc().nullslast(), Job.id.desc())
            .limit(limit)
        )
    )


def short_error(job: Job) -> str:
    text = (job.last_error or "").strip()
    return text if len(text) <= ERROR_LIMIT else text[:ERROR_LIMIT] + "…"


def retry_job(session: Session, operator: Operator, job_id: int) -> Job:
    """Put a dead (or failed) job back in the queue with a fresh set of attempts. The job's
    ``dedupe_key`` is kept, so a retry cannot create a second copy of the work."""
    job = session.scalars(select(Job).where(Job.id == job_id).with_for_update()).first()
    if job is None:
        raise JobError("no such job")
    if job.status not in ("dead", "failed"):
        raise JobError(f"only a dead or failed job can be retried; this one is {job.status}")
    before = {"status": job.status, "attempts": job.attempts, "last_error": short_error(job)}
    session.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(
            status="queued",
            attempts=0,
            run_at=func.now(),
            finished_at=None,
            locked_by=None,
            lease_until=None,
        )
    )
    session.refresh(job)
    audit.record(
        session,
        operator,
        "job.retry",
        "job",
        job.id,
        before=before,
        after={"kind": job.kind, "status": "queued", "attempts": 0},
    )
    return job
