"""Bounded cleanup of completed payloads without erasing dedupe or financial history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

COMPLETED_JOB_RETENTION_DAYS = 90
BATCH_SIZE = 1000


def archive_completed(
    session: Session, now: datetime | None = None, *, batch: int = BATCH_SIZE
) -> int:
    if not 1 <= batch <= BATCH_SIZE:
        raise ValueError("retention batch must be between 1 and 1000")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=COMPLETED_JOB_RETENTION_DAYS)
    # Dead jobs remain available for diagnosis and manual retry. Uncertain provider spend
    # also pins the job until reconciliation; monetary records themselves are retained.
    rows = (
        session.execute(
            text("""
        SELECT id FROM job
        WHERE status = 'done' AND finished_at < :cutoff
          AND NOT EXISTS (SELECT 1 FROM llm_budget_reservation r
                          WHERE r.job_id = job.id AND r.state <> 'settled')
        ORDER BY finished_at, id LIMIT :batch FOR UPDATE SKIP LOCKED
    """),
            {"cutoff": cutoff, "batch": batch},
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    session.execute(
        text("""
        INSERT INTO job_deduplication (dedupe_key, created_at)
        SELECT dedupe_key, created_at FROM job WHERE id = ANY(:ids) AND dedupe_key IS NOT NULL
        ON CONFLICT DO NOTHING
    """),
        {"ids": rows},
    )
    session.execute(
        text("""
        WITH removed AS (
            DELETE FROM job WHERE id = ANY(:ids)
            RETURNING kind, status, attempts, finished_at
        )
        INSERT INTO job_archive (day, kind, status, jobs, attempts)
        SELECT (finished_at AT TIME ZONE 'UTC')::date, kind, status, count(*), sum(attempts)
        FROM removed GROUP BY 1, kind, status
        ON CONFLICT (day, kind, status) DO UPDATE
        SET jobs = job_archive.jobs + EXCLUDED.jobs,
            attempts = job_archive.attempts + EXCLUDED.attempts
    """),
        {"ids": rows},
    )
    return len(rows)


def prune_limits(session: Session, *, batch: int = BATCH_SIZE) -> int:
    if not 1 <= batch <= BATCH_SIZE:
        raise ValueError("retention batch must be between 1 and 1000")
    result = session.execute(
        text("""
        DELETE FROM rate_limit_state WHERE (scope, key_hash) IN (
            SELECT scope, key_hash FROM rate_limit_state
            WHERE expires_at < clock_timestamp() ORDER BY expires_at
            LIMIT :batch FOR UPDATE SKIP LOCKED
        )
    """),
        {"batch": batch},
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
