"""Job queue operations. All functions run inside the caller's transaction.

Callers commit. Claiming uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers never receive
the same job.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

DEFAULT_LEASE_SECONDS = 300
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600
BACKOFF_JITTER = 0.2

log = logging.getLogger("africasignal.jobs.queue")


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_token: UUID | None = None
    lock_owner: str | None = None


def backoff_seconds(attempts: int, rng: random.Random | None = None) -> float:
    """30 s x 2^attempts, capped at 1 h, with +-20 % jitter."""
    base = min(BACKOFF_BASE_SECONDS * 2**attempts, BACKOFF_CAP_SECONDS)
    r = rng or random.Random()
    return float(base * (1 + r.uniform(-BACKOFF_JITTER, BACKOFF_JITTER)))


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    run_at: datetime | None = None,
    max_attempts: int = 5,
) -> int | None:
    """Insert a job. Returns its id, or ``None`` when ``dedupe_key`` already exists."""
    if dedupe_key is not None:
        registered = session.execute(
            text(
                "INSERT INTO job_deduplication (dedupe_key) VALUES (:key) "
                "ON CONFLICT DO NOTHING RETURNING dedupe_key"
            ),
            {"key": dedupe_key},
        ).scalar_one_or_none()
        if registered is None:
            return None
    row = session.execute(
        text(
            """
            INSERT INTO job (kind, payload, dedupe_key, run_at, max_attempts)
            VALUES (:kind, CAST(:payload AS jsonb), :dedupe_key, COALESCE(:run_at, now()), :max)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING id
            """
        ),
        {
            "kind": kind,
            "payload": json.dumps(payload or {}),
            "dedupe_key": dedupe_key,
            "run_at": run_at,
            "max": max_attempts,
        },
    ).scalar_one_or_none()
    return None if row is None else int(row)


def claim(
    session: Session, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> ClaimedJob | None:
    """Atomically claim one due, eligible job using distributed workload-control locks."""
    from datetime import UTC, datetime

    from africasignal import audit
    from africasignal.jobs import handlers as handler_registry
    from africasignal.jobs.policy import blocked_kinds, locked_workload_rows

    # A job with no registered handler is quarantined before it can consume a worker lease. The
    # health monitor raises the existing dead-job alert; neither payload nor arguments are logged.
    # Handler discovery belongs to worker startup. Keep claim bound to the already configured
    # registry so embedded workers and tests can scope their supported job kinds explicitly.
    supported = sorted(handler_registry.HANDLERS)
    if not supported:
        raise RuntimeError("no job handlers registered; refusing to claim queued work")
    known_params = {f"known_{i}": kind for i, kind in enumerate(supported)}
    known_clause = ", ".join(f":known_{i}" for i in range(len(supported)))
    session.execute(text("SELECT pg_advisory_xact_lock(705420036)"))
    unknown = (
        session.execute(
            text(
                f"SELECT id, kind FROM job WHERE status = 'queued' "
                f"AND kind NOT IN ({known_clause}) ORDER BY id FOR UPDATE SKIP LOCKED"
            ),
            known_params,
        )
        .mappings()
        .all()
    )
    for row in unknown:
        changed = cast(
            CursorResult[Any],
            session.execute(
                text(
                    "UPDATE job SET status = 'dead', finished_at = now(), "
                    "last_error = 'unregistered job kind; quarantined by admission' "
                    "WHERE id = :id AND status = 'queued'"
                ),
                {"id": row["id"]},
            ),
        )
        if changed.rowcount:
            audit.record_system(
                session,
                "job.quarantine_unknown_kind",
                "job",
                int(row["id"]),
                before={"kind": row["kind"], "status": "queued"},
                after={"kind": row["kind"], "status": "dead"},
            )
            log.warning(
                "quarantined queued job with no handler",
                extra={"job_id": row["id"], "kind": row["kind"]},
            )

    # Every replica takes these locks in the same order before counting active jobs.
    controls = locked_workload_rows(session)
    excluded = sorted(blocked_kinds(session, controls, datetime.now(UTC)))
    exclusions = "".join(f" AND kind <> :excluded_{i}" for i in range(len(excluded)))
    token = uuid4()
    lock_owner = f"{worker_id}:{token.hex}"
    statement = f"""
        UPDATE job SET status = 'running', locked_by = :lock_owner,
            lease_token = CAST(:lease_token AS uuid),
            lease_until = now() + make_interval(secs => :lease),
            attempts = attempts + 1
        WHERE id = (
            SELECT id FROM job
            WHERE status = 'queued' AND run_at <= now()
                {exclusions}
            ORDER BY run_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, kind, payload, attempts, max_attempts, lease_token, locked_by
    """
    claimed_row = (
        session.execute(
            text(statement),
            {
                "lock_owner": lock_owner,
                "lease_token": str(token),
                "lease": lease_seconds,
                **{f"excluded_{i}": kind for i, kind in enumerate(excluded)},
            },
        )
        .mappings()
        .one_or_none()
    )
    if claimed_row is None:
        return None
    return ClaimedJob(
        id=claimed_row["id"],
        kind=claimed_row["kind"],
        payload=claimed_row["payload"],
        attempts=claimed_row["attempts"],
        max_attempts=claimed_row["max_attempts"],
        lease_token=claimed_row["lease_token"],
        lock_owner=claimed_row["locked_by"],
    )


def defer_claimed(
    session: Session,
    job_id: int,
    lock_owner: str | None,
    lease_token: UUID | None,
    run_at: datetime,
    reason: str,
) -> bool:
    """Return a policy-deferred claim to the queue without consuming an attempt."""
    if lock_owner is None or lease_token is None:
        return False
    result = session.execute(
        text(
            """
            UPDATE job SET status = 'queued', attempts = GREATEST(attempts - 1, 0),
                locked_by = NULL, lease_token = NULL, lease_until = NULL,
                run_at = :run_at, last_error = :reason, finished_at = NULL
            WHERE id = :id AND status = 'running' AND locked_by = :lock_owner
                AND lease_token = CAST(:lease_token AS uuid)
                AND lease_until > clock_timestamp()
            """
        ),
        {
            "id": job_id,
            "lock_owner": lock_owner,
            "lease_token": str(lease_token),
            "run_at": run_at,
            "reason": reason[:500],
        },
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def complete(
    session: Session, job_id: int, lock_owner: str | None, lease_token: UUID | None
) -> bool:
    """Mark an attempt done; stale attempts cannot finish a reclaimed job."""
    if lock_owner is None or lease_token is None:
        return False
    result = session.execute(
        text(
            """
            UPDATE job SET status = 'done', finished_at = now(), locked_by = NULL,
                lease_token = NULL, lease_until = NULL, last_error = NULL
            WHERE id = :id AND status = 'running' AND locked_by = :lock_owner
                AND lease_token = CAST(:lease_token AS uuid)
                AND lease_until > clock_timestamp()
            """
        ),
        {"id": job_id, "lock_owner": lock_owner, "lease_token": str(lease_token)},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def fail(
    session: Session,
    job_id: int,
    lock_owner: str | None,
    lease_token: UUID | None,
    error: str,
    rng: random.Random | None = None,
) -> str | None:
    """Record a failure. Requeues with backoff, or marks the job ``dead`` when attempts are used up.

    Returns the new status, or ``None`` when this worker no longer owns the job.
    """
    if lock_owner is None or lease_token is None:
        return None
    row = (
        session.execute(
            text(
                "SELECT attempts, max_attempts FROM job "
                "WHERE id = :id AND status = 'running' AND locked_by = :lock_owner "
                "AND lease_token = CAST(:lease_token AS uuid) "
                "AND lease_until > clock_timestamp() FOR UPDATE"
            ),
            {"id": job_id, "lock_owner": lock_owner, "lease_token": str(lease_token)},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if row["attempts"] < row["max_attempts"]:
        session.execute(
            text(
                """
                UPDATE job SET status = 'queued', locked_by = NULL, lease_token = NULL,
                    lease_until = NULL, last_error = :error,
                    run_at = now() + make_interval(secs => :delay)
                WHERE id = :id
                """
            ),
            {"id": job_id, "error": error, "delay": backoff_seconds(row["attempts"], rng)},
        )
        return "queued"
    session.execute(
        text(
            """
            UPDATE job SET status = 'dead', locked_by = NULL, lease_token = NULL,
                lease_until = NULL, last_error = :error, finished_at = now()
            WHERE id = :id
            """
        ),
        {"id": job_id, "error": error},
    )
    return "dead"


def extend_lease(
    session: Session,
    job_id: int,
    lock_owner: str | None,
    lease_token: UUID | None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    if lock_owner is None or lease_token is None:
        return False
    result = session.execute(
        text(
            """
            UPDATE job SET lease_until = clock_timestamp() + make_interval(secs => :lease)
            WHERE id = :id AND status = 'running' AND locked_by = :lock_owner
                AND lease_token = CAST(:lease_token AS uuid)
                AND lease_until > clock_timestamp()
            """
        ),
        {
            "id": job_id,
            "lock_owner": lock_owner,
            "lease_token": str(lease_token),
            "lease": lease_seconds,
        },
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def reclaim_expired(session: Session) -> int:
    """Return jobs whose lease ran out to the queue (or to ``dead`` if attempts are used up)."""
    result = session.execute(
        text(
            """
            UPDATE job SET
                status = CASE WHEN attempts < max_attempts THEN 'queued'::job_status
                              ELSE 'dead'::job_status END,
                finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
                last_error = 'lease expired',
                locked_by = NULL, lease_token = NULL, lease_until = NULL
            WHERE status = 'running' AND lease_until < now()
            """
        )
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
