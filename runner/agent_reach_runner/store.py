"""Durable, app-independent task state and per-day search-call accounting."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agent_reach_runner.schemas import RunnerCandidate, TaskResponse, TaskSubmission


def youtube_quota_day(moment: datetime | None = None) -> str:
    at = moment or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("quota timestamps must include a timezone")
    return at.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()


class _ClosingConnection(sqlite3.Connection):
    """Close SQLite handles after their transaction context completes."""

    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class TaskConflict(ValueError):
    """A task or idempotency key was reused for a different request."""


class TaskStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=5, isolation_level=None, factory=_ClosingConnection
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialise(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runner_task (
                    external_task_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN
                        ('queued','running','succeeded','failed','cancelled','expired')),
                    results_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS youtube_search_quota (
                    quota_day TEXT PRIMARY KEY,
                    searches_reserved INTEGER NOT NULL CHECK (searches_reserved >= 0)
                );                CREATE INDEX IF NOT EXISTS ix_runner_task_status_created
                    ON runner_task(status, created_at);
                CREATE TABLE IF NOT EXISTS daily_search_usage (
                    utc_day TEXT PRIMARY KEY,
                    searches_reserved INTEGER NOT NULL CHECK (searches_reserved >= 0)
                );
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _request_json(request: TaskSubmission) -> str:
        return json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def enqueue(self, request: TaskSubmission) -> TaskResponse:
        payload_json = self._request_json(request)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM runner_task WHERE idempotency_key = ? OR task_id = ?",
                (request.idempotency_key, request.task_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["idempotency_key"] != request.idempotency_key
                    or existing["payload_hash"] != payload_hash
                ):
                    raise TaskConflict(
                        "task identity or idempotency key was reused for another request"
                    )
                return self._response(existing)
            queued = connection.execute(
                "SELECT COUNT(*) FROM runner_task WHERE status IN ('queued','running')"
            ).fetchone()[0]
            if queued >= 100:
                raise TaskConflict("runner queue is at its bounded capacity")
            external_task_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO runner_task (
                    external_task_id,task_id,idempotency_key,payload_hash,payload_json,status,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,'queued',?,?)""",
                (
                    external_task_id,
                    request.task_id,
                    request.idempotency_key,
                    payload_hash,
                    payload_json,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runner_task WHERE external_task_id = ?", (external_task_id,)
            ).fetchone()
            return self._response(row)

    def claim_next(self) -> tuple[str, TaskSubmission] | None:
        while True:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM runner_task WHERE status='queued' ORDER BY created_at LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                request = TaskSubmission.model_validate(json.loads(row["payload_json"]))
                now = datetime.now(UTC)
                if request.deadline_at.astimezone(UTC) <= now:
                    connection.execute(
                        "UPDATE runner_task SET status='expired', error=?, updated_at=? "
                        "WHERE external_task_id=?",
                        (
                            "Task deadline elapsed before execution.",
                            now.isoformat(),
                            row["external_task_id"],
                        ),
                    )
                    continue
                connection.execute(
                    "UPDATE runner_task SET status='running', attempts=attempts+1, updated_at=? "
                    "WHERE external_task_id=?",
                    (now.isoformat(), row["external_task_id"]),
                )
                return row["external_task_id"], request

    def recover_interrupted(self) -> None:
        """Fence work interrupted in flight; its provider outcome may already be billable."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE runner_task SET status='cancelled', error=?, updated_at=?
                   WHERE status='running' AND cancel_requested=1""",
                ("Task was cancelled while the runner restarted.", now),
            )
            connection.execute(
                """UPDATE runner_task SET status='failed', error=?, updated_at=?
                   WHERE status='running' AND cancel_requested=0""",
                (
                    "Provider outcome is unknown after runner restart; "
                    "no automatic retry was issued.",
                    now,
                ),
            )

    def reserve_search(self, daily_limit: int) -> bool:
        # YouTube's search.list quota bucket resets at midnight Pacific Time, not UTC.
        quota_day = youtube_quota_day()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT searches_reserved FROM youtube_search_quota WHERE quota_day=?",
                (quota_day,),
            ).fetchone()
            used = int(row[0]) if row else 0
            if used >= daily_limit:
                return False
            if row is None:
                connection.execute(
                    "INSERT INTO youtube_search_quota (quota_day,searches_reserved) VALUES (?,1)",
                    (quota_day,),
                )
            else:
                connection.execute(
                    "UPDATE youtube_search_quota SET searches_reserved=searches_reserved+1 "
                    "WHERE quota_day=?",
                    (quota_day,),
                )
            return True

    def request_cancel(self, external_task_id: str) -> TaskResponse | None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runner_task WHERE external_task_id=?", (external_task_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "queued":
                connection.execute(
                    "UPDATE runner_task SET status='cancelled', updated_at=? "
                    "WHERE external_task_id=?",
                    (now, external_task_id),
                )
            elif row["status"] == "running":
                connection.execute(
                    "UPDATE runner_task SET cancel_requested=1, updated_at=? "
                    "WHERE external_task_id=?",
                    (now, external_task_id),
                )
            row = connection.execute(
                "SELECT * FROM runner_task WHERE external_task_id=?", (external_task_id,)
            ).fetchone()
            return self._response(row)

    def cancel_requested(self, external_task_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM runner_task WHERE external_task_id=?",
                (external_task_id,),
            ).fetchone()
            return row is None or bool(row[0])

    def complete(self, external_task_id: str, results: list[RunnerCandidate]) -> None:
        now = datetime.now(UTC).isoformat()
        results_json = json.dumps(
            [item.model_dump(mode="json") for item in results],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT cancel_requested,status FROM runner_task WHERE external_task_id=?",
                (external_task_id,),
            ).fetchone()
            if row is None or row["status"] != "running":
                return
            if row["cancel_requested"]:
                connection.execute(
                    "UPDATE runner_task SET status='cancelled', error=?, updated_at=? "
                    "WHERE external_task_id=?",
                    ("Task cancelled; search results were discarded.", now, external_task_id),
                )
            else:
                connection.execute(
                    "UPDATE runner_task SET status='succeeded', results_json=?, "
                    "error=NULL, updated_at=? "
                    "WHERE external_task_id=?",
                    (results_json, now, external_task_id),
                )

    def expire(self, external_task_id: str, message: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM runner_task WHERE external_task_id=?", (external_task_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                return
            connection.execute(
                "UPDATE runner_task SET status='expired', error=?, updated_at=? "
                "WHERE external_task_id=?",
                (message[:300], now, external_task_id),
            )

    def fail(self, external_task_id: str, message: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT cancel_requested,status FROM runner_task WHERE external_task_id=?",
                (external_task_id,),
            ).fetchone()
            if row is None or row["status"] != "running":
                return
            status = "cancelled" if row["cancel_requested"] else "failed"
            error = "Task cancelled." if row["cancel_requested"] else message[:300]
            connection.execute(
                "UPDATE runner_task SET status=?, error=?, updated_at=? WHERE external_task_id=?",
                (status, error, now, external_task_id),
            )

    def get(self, external_task_id: str) -> TaskResponse | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runner_task WHERE external_task_id=?", (external_task_id,)
            ).fetchone()
            return self._response(row) if row is not None else None

    @staticmethod
    def _response(row: sqlite3.Row) -> TaskResponse:
        request = TaskSubmission.model_validate(json.loads(row["payload_json"]))
        results: list[RunnerCandidate] = []
        if row["status"] == "succeeded":
            results = [
                RunnerCandidate.model_validate(item) for item in json.loads(row["results_json"])
            ]
        return TaskResponse(
            external_task_id=row["external_task_id"],
            task_id=request.task_id,
            control_generation=request.control_generation,
            config_revision=request.config_revision,
            status=row["status"],
            results=results,
            error=row["error"],
        )

    def purge_expired_results(self, retention_days: int = 28) -> int:
        """Delete provider metadata and queries while retaining idempotency tombstones."""
        if not 1 <= retention_days <= 28:
            raise ValueError("result retention must be between 1 and 28 days")
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT external_task_id,payload_json FROM runner_task "
                "WHERE status IN ('succeeded','failed','cancelled','expired') "
                "AND created_at <= ? "
                "AND error IS NOT 'Task data expired under the retention policy.'",
                (cutoff,),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, ValueError):
                    # A corrupt terminal payload cannot be safely retained or retried.
                    connection.execute(
                        "DELETE FROM runner_task WHERE external_task_id=?",
                        (row["external_task_id"],),
                    )
                    continue
                # Keep the original payload hash and stable IDs so retries cannot create a second
                # paid search, but remove the operator query and all provider result metadata.
                payload["query"] = "[expired query]"
                connection.execute(
                    """UPDATE runner_task SET status='expired', results_json='[]',
                       error='Task data expired under the retention policy.',
                       payload_json=?, updated_at=?
                       WHERE external_task_id=?""",
                    (
                        json.dumps(
                            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ),
                        now,
                        row["external_task_id"],
                    ),
                )
            usage_cutoff = (datetime.now(UTC).date() - timedelta(days=90)).isoformat()
            connection.execute("DELETE FROM daily_search_usage WHERE utc_day < ?", (usage_cutoff,))
            connection.execute(
                "DELETE FROM youtube_search_quota WHERE quota_day < ?", (usage_cutoff,)
            )
            return len(rows)

    def get_by_task_id(self, task_id: str) -> TaskResponse | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runner_task WHERE task_id=?", (task_id,)
            ).fetchone()
            return self._response(row) if row is not None else None

    def pending_count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM runner_task WHERE status IN ('queued','running')"
                ).fetchone()[0]
            )
