"""Real PostgreSQL checks for replica limits, lease races and retention."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from africasignal.jobs import handlers, queue
from africasignal.jobs.execution import Attempt, LeaseLost, checkpoint, executing, fenced_effect
from africasignal.jobs.retention import archive_completed, prune_limits
from africasignal.shared_limits import SharedLimits


def test_independent_replicas_share_one_sliding_window(engine):
    key = uuid4().hex

    def request(_):
        return SharedLimits(engine).take("test", key, rate=0.05, capacity=3, window=60)[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(request, range(20))) == 3
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT hits FROM rate_limit_state WHERE key_hash=:key"), {"key": key}
        ).scalar_one()
        assert len(row) == 3


def test_crawl_configuration_change_does_not_refill_or_relax(engine):
    key = uuid4().hex
    backend = SharedLimits(engine)
    assert backend.take("crawl", key, rate=1 / 3600, capacity=1)[0]
    assert not SharedLimits(engine).take("crawl", key, rate=1000, capacity=1000)[0]
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE rate_limit_state SET touched_at=clock_timestamp()-interval '2 hours' WHERE key_hash=:key"
            ),
            {"key": key},
        )
    assert backend.take("crawl", key, rate=1 / 3600, capacity=1)[0]


def _claim(engine, monkeypatch, lease_seconds=300):
    monkeypatch.setitem(handlers.HANDLERS, "lease_probe", lambda ctx: None)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.execute(text("DELETE FROM job"))
        job_id = queue.enqueue(session, "lease_probe")
        session.commit()
        job = queue.claim(session, "probe", lease_seconds=lease_seconds)
        session.commit()
    assert job is not None and job.id == job_id
    return factory, job


def test_stale_attempt_cannot_commit_or_dispatch(engine, monkeypatch):
    factory, job = _claim(engine, monkeypatch)
    lost = threading.Event()
    with factory() as session:
        with executing(Attempt(job, factory, lost), session):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE job SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"
                    ),
                    {"id": job.id},
                )
            with pytest.raises(LeaseLost):
                with fenced_effect():
                    pytest.fail("stale attempt dispatched")
            session.execute(
                text(
                    "INSERT INTO job_archive (day,kind,status,jobs,attempts) VALUES ('2000-01-01','probe','done',1,1)"
                )
            )
            with pytest.raises(LeaseLost):
                session.commit()
            session.rollback()
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM job_archive WHERE kind='probe'")).scalar_one()
            == 0
        )


def test_effect_fence_blocks_reclaim_and_composes_with_nested_storage(engine, monkeypatch):
    factory, job = _claim(engine, monkeypatch)
    with factory() as session:
        with executing(Attempt(job, factory, threading.Event()), session):
            with fenced_effect():
                with fenced_effect():
                    with engine.begin() as conn:
                        conn.execute(text("SET LOCAL lock_timeout = '100ms'"))
                        with pytest.raises(Exception, match="lock timeout"):
                            conn.execute(
                                text(
                                    "UPDATE job SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"
                                ),
                                {"id": job.id},
                            )
            checkpoint()


def test_archive_preserves_dedupe_money_and_unresolved_jobs(engine, monkeypatch):
    now = datetime.now(UTC)
    key = uuid4().hex
    factory = sessionmaker(engine)
    with factory() as session:
        session.execute(text("DELETE FROM job"))
        job_id = queue.enqueue(session, "probe", {"private_payload": "remove-me"}, dedupe_key=key)
        session.execute(
            text("UPDATE job SET status='done', attempts=2, finished_at=:at WHERE id=:id"),
            {"id": job_id, "at": now - timedelta(days=91)},
        )
        session.execute(
            text(
                "INSERT INTO llm_call (purpose,model_id,prompt_version,input_tokens,output_tokens,cost_usd,job_id) VALUES ('probe','probe','1',1,1,0.1,:id)"
            ),
            {"id": job_id},
        )
        dead = queue.enqueue(session, "probe")
        session.execute(
            text("UPDATE job SET status='dead', finished_at=:at WHERE id=:id"),
            {"id": dead, "at": now - timedelta(days=400)},
        )
        assert archive_completed(session, now) == 1
        assert archive_completed(session, now) == 0
        assert queue.enqueue(session, "probe", dedupe_key=key) is None
        session.commit()
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM job WHERE id=:id"), {"id": dead}).scalar_one()
            == 1
        )
        assert (
            conn.execute(text("SELECT job_id FROM llm_call WHERE purpose='probe'")).scalar_one()
            is None
        )
        assert (
            conn.execute(text("SELECT jobs FROM job_archive WHERE kind='probe'")).scalar_one() == 1
        )


def test_limit_pruning_is_bounded_and_preserves_active_entries(engine):
    with Session(engine) as session:
        session.execute(text("DELETE FROM rate_limit_state"))
        for key in ("old1", "old2"):
            session.execute(
                text(
                    "INSERT INTO rate_limit_state (scope,key_hash,expires_at) VALUES ('test',:key,clock_timestamp()-interval '1 second')"
                ),
                {"key": key},
            )
        session.execute(
            text(
                "INSERT INTO rate_limit_state (scope,key_hash,expires_at) VALUES ('test','live',clock_timestamp()+interval '1 hour')"
            )
        )
        assert prune_limits(session, batch=1) == 1
        assert session.execute(text("SELECT count(*) FROM rate_limit_state")).scalar_one() == 2


def test_reclaim_waits_for_dispatch_fence(engine, monkeypatch):
    import time

    factory, job = _claim(engine, monkeypatch, lease_seconds=1)
    with factory() as session:
        with executing(Attempt(job, factory, threading.Event()), session):
            with fenced_effect():
                time.sleep(1.1)
                with Session(engine) as replacement:
                    replacement.execute(text("SET LOCAL lock_timeout='100ms'"))
                    with pytest.raises(Exception, match="lock timeout"):
                        queue.reclaim_expired(replacement)
                    replacement.rollback()
            with Session(engine) as replacement:
                assert queue.reclaim_expired(replacement) == 1
                replacement.commit()
            with pytest.raises(LeaseLost):
                checkpoint()


def test_public_limiters_share_scope_and_store_no_client_address(engine, monkeypatch):
    from types import SimpleNamespace

    from africasignal import shared_limits
    from africasignal.web import ratelimit

    monkeypatch.setattr(ratelimit, "shared_enabled", lambda: True)
    monkeypatch.setattr(ratelimit, "SharedLimits", lambda: SharedLimits(engine))
    monkeypatch.setattr(
        shared_limits, "get_settings", lambda: SimpleNamespace(secret_key="test-secret")
    )
    first = ratelimit.RateLimiter(limit=1, scope="api-probe")
    second = ratelimit.RateLimiter(limit=1, scope="api-probe")
    assert first.check("203.0.113.47")[0]
    assert not second.check("203.0.113.47")[0]
    with engine.connect() as conn:
        key = conn.execute(
            text("SELECT key_hash FROM rate_limit_state WHERE scope='api-probe'")
        ).scalar_one()
        assert len(key) == 64 and "203.0.113.47" not in key


def test_retention_backlog_is_visible_and_resolves(session):
    from africasignal import health_alerts

    now = datetime.now(UTC)
    session.execute(
        text("INSERT INTO job (kind,status,finished_at) VALUES ('probe','done',:at)"),
        {"at": now - timedelta(days=92)},
    )
    codes = {finding.code for finding in health_alerts.evaluate(session, now)}
    assert health_alerts.RETENTION_BACKLOG in codes
    archive_completed(session, now)
    codes = {finding.code for finding in health_alerts.evaluate(session, now)}
    assert health_alerts.RETENTION_BACKLOG not in codes


def test_upgrade_backfills_existing_deduplication_keys(engine):
    from alembic import command
    from tests.integration.conftest import alembic_config

    command.downgrade(alembic_config(), "0046")
    key = uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO job (kind,dedupe_key,status,finished_at) VALUES ('legacy',:key,'done',clock_timestamp()-interval '100 days')"
            ),
            {"key": key},
        )
    command.upgrade(alembic_config(), "head")
    with Session(engine) as session:
        assert queue.enqueue(session, "legacy", dedupe_key=key) is None
        assert (
            session.execute(
                text("SELECT count(*) FROM job_deduplication WHERE dedupe_key=:key"), {"key": key}
            ).scalar_one()
            == 1
        )
    # This test bypasses the shared fixture intentionally while downgrading to 0046. Remove its
    # legacy job and tombstone so later retention tests begin with an empty migrated schema.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM job WHERE dedupe_key=:key"), {"key": key})
        conn.execute(text("DELETE FROM job_deduplication WHERE dedupe_key=:key"), {"key": key})


def test_archive_keeps_unresolved_spend_until_settlement(session):
    cutoff = datetime.now(UTC) - timedelta(days=100)
    for state in ("reserved", "uncertain", "settled"):
        job_id = queue.enqueue(session, "money-probe")
        session.execute(
            text("UPDATE job SET status='done', finished_at=:at WHERE id=:id"),
            {"id": job_id, "at": cutoff},
        )
        session.execute(
            text(
                "INSERT INTO llm_budget_reservation (budget_day,job_id,input_token_bound,output_token_bound,reserved_usd,state) VALUES (clock_timestamp(),:id,1,1,0.1,:state)"
            ),
            {"id": job_id, "state": state},
        )
    assert archive_completed(session) == 1
    assert (
        session.execute(text("SELECT count(*) FROM job WHERE kind='money-probe'")).scalar_one() == 2
    )
    assert (
        session.execute(
            text(
                "SELECT count(*) FROM llm_budget_reservation WHERE job_id IS NULL AND state='settled'"
            )
        ).scalar_one()
        == 1
    )
    session.execute(
        text("UPDATE llm_budget_reservation SET state='settled' WHERE state<>'settled'")
    )
    assert archive_completed(session) == 2
    assert (
        session.execute(
            text("SELECT count(*) FROM llm_budget_reservation WHERE job_id IS NULL")
        ).scalar_one()
        == 3
    )
