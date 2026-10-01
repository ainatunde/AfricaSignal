import json
import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from africasignal.jobs import handlers, queue, scheduler
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.log import JsonFormatter
from africasignal.jobs.worker import Worker


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Sessions that really commit; the job and source tables are emptied around each test."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM job; DELETE FROM source"))
    yield sessionmaker(bind=engine, expire_on_commit=False)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM job; DELETE FROM source"))


def _row(factory: sessionmaker[Session], job_id: int) -> dict[str, object]:
    with factory() as s:
        return dict(
            s.execute(text("SELECT * FROM job WHERE id = :id"), {"id": job_id}).mappings().one()
        )


def _enqueue(factory: sessionmaker[Session], kind: str = "test", **kw: object) -> int:
    with factory() as s:
        job_id = queue.enqueue(s, kind, **kw)  # type: ignore[arg-type]
        s.commit()
    assert job_id is not None
    return job_id


@pytest.fixture(autouse=True)
def no_real_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing a handler module (other test modules do) registers it for good. These tests
    decide which kinds have a handler, so start each from an empty registry."""
    monkeypatch.setattr(handlers, "HANDLERS", {})


def _register(monkeypatch: pytest.MonkeyPatch, kind: str, fn: handlers.Handler) -> None:
    monkeypatch.setitem(handlers.HANDLERS, kind, fn)


def _make_due_now(factory: sessionmaker[Session], job_id: int) -> None:
    with factory() as s:
        s.execute(text("UPDATE job SET run_at = now() WHERE id = :id"), {"id": job_id})
        s.commit()


# --- enqueue -----------------------------------------------------------------------------


def test_enqueue_dedupe_key_makes_second_insert_a_noop(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        first = queue.enqueue(s, "fetch_source", {"source_id": 1}, dedupe_key="fetch:1:5")
        second = queue.enqueue(s, "fetch_source", {"source_id": 1}, dedupe_key="fetch:1:5")
        s.commit()
        n = s.execute(text("SELECT count(*) FROM job")).scalar_one()
    assert first is not None
    assert second is None
    assert n == 1


# --- claiming ----------------------------------------------------------------------------


def test_four_threads_claim_100_jobs_each_exactly_once(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        for i in range(100):
            queue.enqueue(s, "test", {"n": i})
        s.commit()

    claimed: list[int] = []
    lock = threading.Lock()

    def run(worker_id: str) -> None:
        while True:
            with factory() as s:
                job = queue.claim(s, worker_id)
                s.commit()
                if job is None:
                    return
                with lock:
                    claimed.append(job.id)
                queue.complete(s, job.id, worker_id)
                s.commit()

    threads = [threading.Thread(target=run, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 100
    assert len(set(claimed)) == 100
    with factory() as s:
        done = s.execute(text("SELECT count(*) FROM job WHERE status = 'done'")).scalar_one()
    assert done == 100


def test_claim_skips_jobs_not_yet_due(factory: sessionmaker[Session]) -> None:
    _enqueue(factory, run_at=datetime(2999, 1, 1, tzinfo=UTC))
    with factory() as s:
        assert queue.claim(s, "w") is None


# --- leases ------------------------------------------------------------------------------


def test_job_of_killed_worker_is_picked_up_after_lease_expiry(
    factory: sessionmaker[Session],
) -> None:
    job_id = _enqueue(factory)
    with factory() as s:
        assert queue.claim(s, "dying-worker") is not None
        s.commit()
    # Nothing is reclaimed while the lease is valid.
    with factory() as s:
        assert queue.reclaim_expired(s) == 0
        assert queue.claim(s, "other") is None
    with factory() as s:
        s.execute(
            text("UPDATE job SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )
        s.commit()
    with factory() as s:
        assert queue.reclaim_expired(s) == 1
        s.commit()
        job = queue.claim(s, "other")
        s.commit()
    assert job is not None and job.id == job_id and job.attempts == 2


def test_reclaimed_job_with_no_attempts_left_becomes_dead(factory: sessionmaker[Session]) -> None:
    job_id = _enqueue(factory, max_attempts=1)
    with factory() as s:
        queue.claim(s, "w")
        s.execute(text("UPDATE job SET lease_until = now() - interval '1 second'"))
        assert queue.reclaim_expired(s) == 1
        s.commit()
    assert _row(factory, job_id)["status"] == "dead"


def test_stale_worker_cannot_complete_or_fail_a_reclaimed_job(
    factory: sessionmaker[Session],
) -> None:
    job_id = _enqueue(factory)
    with factory() as s:
        queue.claim(s, "old")
        s.execute(text("UPDATE job SET lease_until = now() - interval '1 second'"))
        queue.reclaim_expired(s)
        queue.claim(s, "new")
        s.commit()
    with factory() as s:
        assert queue.complete(s, job_id, "old") is False
        assert queue.fail(s, job_id, "old", "boom") is None
        assert queue.extend_lease(s, job_id, "old") is False
    assert _row(factory, job_id)["status"] == "running"


# --- failure and backoff ------------------------------------------------------------------


def test_backoff_is_exponential_capped_and_jittered() -> None:
    rng = random.Random(1)
    for attempts, base in [(1, 60), (2, 120), (3, 240), (6, 1920), (7, 3600), (20, 3600)]:
        for _ in range(50):
            delay = queue.backoff_seconds(attempts, rng)
            assert base * 0.8 <= delay <= base * 1.2


def test_job_failing_five_times_becomes_dead(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(ctx: JobContext) -> None:
        raise RuntimeError("nope")

    _register(monkeypatch, "boom", boom)
    job_id = _enqueue(factory, "boom")
    worker = Worker(factory, "w")

    for attempt in range(1, 6):
        assert worker.run_once() is True
        row = _row(factory, job_id)
        assert row["attempts"] == attempt
        assert row["last_error"] == "RuntimeError: nope"
        if attempt < 5:
            assert row["status"] == "queued"
            # A failed job waits out its backoff before it can be claimed again.
            assert worker.run_once() is False
            _make_due_now(factory, job_id)
    assert _row(factory, job_id)["status"] == "dead"
    assert worker.run_once() is False


# --- worker --------------------------------------------------------------------------------


def test_worker_runs_handler_and_marks_job_done(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, dict[str, object], int]] = []

    def handler(ctx: JobContext) -> None:
        seen.append((ctx.job.kind, ctx.job.payload, ctx.job.attempts))

    _register(monkeypatch, "echo", handler)
    job_id = _enqueue(factory, "echo", payload={"a": 1})
    assert Worker(factory, "w").run_once() is True
    assert seen == [("echo", {"a": 1}, 1)]
    row = _row(factory, job_id)
    assert row["status"] == "done" and row["finished_at"] is not None


def test_unknown_job_kind_fails_with_clear_error(factory: sessionmaker[Session]) -> None:
    job_id = _enqueue(factory, "no_such_kind")
    Worker(factory, "w").run_once()
    row = _row(factory, job_id)
    assert row["status"] == "queued"
    assert "no handler registered" in str(row["last_error"])


def test_failed_handler_work_is_rolled_back(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(ctx: JobContext) -> None:
        ctx.session.execute(text("INSERT INTO setting (key, value) VALUES ('leak', 'true')"))
        raise RuntimeError("after write")

    _register(monkeypatch, "leaky", handler)
    _enqueue(factory, "leaky")
    Worker(factory, "w").run_once()
    with factory() as s:
        assert s.execute(text("SELECT count(*) FROM setting WHERE key = 'leak'")).scalar_one() == 0


def test_long_handler_extends_its_lease(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[object] = []

    def slow(ctx: JobContext) -> None:
        observed.append(_row(factory, ctx.job.id)["lease_until"])
        time.sleep(0.6)
        observed.append(_row(factory, ctx.job.id)["lease_until"])

    _register(monkeypatch, "slow", slow)
    _enqueue(factory, "slow")
    Worker(factory, "w", heartbeat_seconds=0.1).run_once()
    assert observed[1] > observed[0]  # type: ignore[operator]


def test_stop_lets_the_current_job_finish_then_exits(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    finished: list[bool] = []
    worker = Worker(factory, "w")

    def handler(ctx: JobContext) -> None:
        started.set()
        time.sleep(0.3)
        finished.append(True)

    _register(monkeypatch, "graceful", handler)
    job_id = _enqueue(factory, "graceful")
    _enqueue(factory, "graceful")
    thread = threading.Thread(target=worker.run_forever)
    thread.start()
    assert started.wait(5)
    worker.stop()
    thread.join(5)
    assert not thread.is_alive()
    assert finished == [True]
    assert _row(factory, job_id)["status"] == "done"
    with factory() as s:  # the second job was never started
        assert s.execute(text("SELECT count(*) FROM job WHERE status='queued'")).scalar_one() == 1


def test_sigterm_stops_the_worker_process_cleanly(factory: sessionmaker[Session]) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "africasignal.jobs.worker"],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        first = json.loads(proc.stdout.readline())
        assert first["msg"] == "worker started"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_json_log_line_carries_job_context() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "job started", None, None)
    record.job_id, record.kind, record.attempt = 7, "fetch_source", 2
    line = json.loads(JsonFormatter().format(record))
    assert line["job_id"] == 7 and line["kind"] == "fetch_source" and line["attempt"] == 2
    assert line["msg"] == "job started" and line["level"] == "INFO"


# --- scheduler -----------------------------------------------------------------------------


def _add_source(factory: sessionmaker[Session], *, active: bool = True, due: str = "now()") -> int:
    with factory() as s:
        sid = s.execute(
            text(
                "INSERT INTO source"
                " (slug, name, kind, adapter, schedule_minutes, next_due_at, active)"
                " VALUES (:slug, 'S', 'official_statistics', 'nbs', 60, "
                + due
                + ", :active) RETURNING id"
            ),
            {"slug": f"s{time.time_ns()}", "active": active},
        ).scalar_one()
        s.commit()
    return int(sid)


def _tick(factory: sessionmaker[Session], now: datetime | None = None) -> dict[str, int]:
    with factory() as s:
        counts = scheduler.tick(s, now)
        s.commit()
    return counts


def _kinds(factory: sessionmaker[Session]) -> list[str]:
    with factory() as s:
        return sorted(s.execute(text("SELECT kind FROM job")).scalars())


def test_due_source_is_enqueued_once_and_pushed_forward(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, "fetch_source", lambda ctx: None)
    sid = _add_source(factory, due="now() - interval '5 minutes'")
    assert _tick(factory)["fetch_source"] == 1
    assert _tick(factory)["fetch_source"] == 0  # next_due_at is now in the future
    with factory() as s:
        row = s.execute(
            text("SELECT payload, dedupe_key FROM job WHERE kind = 'fetch_source'")
        ).one()
        assert row.payload == {"source_id": sid}
        assert row.dedupe_key.startswith(f"fetch:{sid}:")
        assert s.execute(
            text("SELECT next_due_at > now() + interval '59 minutes' FROM source")
        ).scalar_one()


def test_same_due_slot_is_never_enqueued_twice(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, "fetch_source", lambda ctx: None)
    sid = _add_source(factory, due="now() - interval '3 hours'")
    with factory() as s:
        due = s.execute(
            text("SELECT next_due_at FROM source WHERE id = :id"), {"id": sid}
        ).scalar_one()
    _tick(factory)
    # A scheduler restart that sees the same stale next_due_at derives the same dedupe key.
    with factory() as s:
        s.execute(
            text("UPDATE source SET next_due_at = :due WHERE id = :id"), {"due": due, "id": sid}
        )
        s.commit()
    assert _tick(factory)["fetch_source"] == 0
    assert _kinds(factory) == ["fetch_source"]


def test_inactive_and_not_due_sources_are_skipped(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, "fetch_source", lambda ctx: None)
    _add_source(factory, active=False, due="now() - interval '1 hour'")
    _add_source(factory, due="now() + interval '1 hour'")
    assert _tick(factory)["fetch_source"] == 0
    assert "fetch_source" not in _kinds(factory)


def test_kinds_without_a_handler_are_not_enqueued(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "HANDLERS", {})
    for kind in ("fetch_source", "gdelt_poll", "expire_assessments", "dispatch_outbox"):
        assert handlers.get_handler(kind) is None
    _add_source(factory, due="now() - interval '1 hour'")
    _tick(factory)
    assert _kinds(factory) == []


def test_check_backups_is_enqueued_once_an_hour(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, "check_backups", lambda ctx: None)
    t = datetime(2026, 10, 7, 10, 3, 10, tzinfo=UTC)
    _tick(factory, t)
    _tick(factory, t.replace(minute=50))
    assert _kinds(factory) == ["check_backups"]
    _tick(factory, t.replace(hour=11))
    assert _kinds(factory) == ["check_backups", "check_backups"]


def test_periodic_jobs_are_deduplicated_per_slot(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    for kind in ("gdelt_poll", "expire_assessments", "dispatch_outbox"):
        _register(monkeypatch, kind, lambda ctx: None)
    t = datetime(2026, 10, 7, 10, 3, 10, tzinfo=UTC)  # a Wednesday
    _tick(factory, t)
    _tick(factory, t.replace(second=50))  # same minute, same slots
    assert _kinds(factory) == ["dispatch_outbox", "expire_assessments", "gdelt_poll"]
    _tick(factory, t.replace(minute=4))  # next minute: only dispatch_outbox is due again
    assert _kinds(factory).count("dispatch_outbox") == 2
    assert _kinds(factory).count("gdelt_poll") == 1
    _tick(factory, t.replace(minute=20))  # next 15-minute slot
    assert _kinds(factory).count("gdelt_poll") == 2


@pytest.mark.parametrize(
    ("now_utc", "expected"),
    [
        (datetime(2026, 10, 5, 5, 59, tzinfo=UTC), 0),  # Mon 06:59 Lagos (UTC+1)
        (datetime(2026, 10, 5, 6, 0, tzinfo=UTC), 1),  # Mon 07:00 Lagos
        (datetime(2026, 10, 5, 12, 0, tzinfo=UTC), 1),
        (datetime(2026, 10, 6, 6, 0, tzinfo=UTC), 0),  # Tuesday
    ],
)
def test_weekly_digest_runs_monday_0700_lagos(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    now_utc: datetime,
    expected: int,
) -> None:
    _register(monkeypatch, "weekly_digest", lambda ctx: None)
    _tick(factory, now_utc)
    _tick(factory, now_utc)
    assert _kinds(factory).count("weekly_digest") == expected


def test_scheduler_reclaims_expired_leases(factory: sessionmaker[Session]) -> None:
    job_id = _enqueue(factory)
    with factory() as s:
        queue.claim(s, "w")
        s.execute(text("UPDATE job SET lease_until = now() - interval '1 second'"))
        s.commit()
    assert _tick(factory)["reclaimed"] == 1
    assert _row(factory, job_id)["status"] == "queued"


def test_held_versions_are_checked_every_minute(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, "release_held_versions", lambda ctx: None)
    t = datetime(2026, 10, 7, 10, 3, 10, tzinfo=UTC)
    _tick(factory, t)
    _tick(factory, t.replace(second=50))  # same minute
    assert _kinds(factory) == ["release_held_versions"]
    _tick(factory, t.replace(minute=4))
    assert _kinds(factory) == ["release_held_versions"] * 2


def test_check_health_is_enqueued_every_fifteen_minutes(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, "check_health", lambda ctx: None)
    t = datetime(2026, 10, 7, 10, 3, 10, tzinfo=UTC)
    _tick(factory, t)
    _tick(factory, t.replace(minute=10))
    assert _kinds(factory) == ["check_health"]
    _tick(factory, t.replace(minute=20))
    assert _kinds(factory) == ["check_health", "check_health"]
