"""Alerts on failing sources, dead jobs and the model budget (AS-041): ``health_alerts`` and the
``check_health`` job."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from africasignal import backup_alerts, health_alerts, settings_store, storage
from africasignal.jobs import handlers
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.queue import ClaimedJob
from africasignal.models import (
    AuditLog,
    GuardianRepairAttempt,
    Job,
    LlmCall,
    Outbox,
    Series,
    Setting,
    Source,
    SourcePermission,
)
from tests.integration.test_admin_console import make_operator

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean(session: Session) -> None:
    session.execute(delete(AuditLog))
    session.execute(delete(Setting).where(Setting.key.like("ops.%")))
    session.flush()


def source(session: Session, slug: str, health: str, *, active: bool = True) -> Source:
    row = Source(
        slug=slug,
        name=slug,
        kind="news_outlet",
        adapter="rss",
        schedule_minutes=30,
        active=active,
        health=health,
        consecutive_failures=5 if health == "failing" else 0,
        last_error="HTTP 503 from the server",
    )
    session.add(row)
    session.flush()
    return row


def dead_job(session: Session, kind: str, finished: datetime, status: str = "dead") -> Job:
    job = Job(
        kind=kind, payload={"email": "reader@example.org"}, status=status, finished_at=finished
    )
    session.add(job)
    session.flush()
    return job


def spend(session: Session, usd: str) -> None:
    session.add(
        LlmCall(
            purpose="explain",
            model_id="m",
            prompt_version="v1",
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal(usd),
            ts=NOW - timedelta(hours=1),
        )
    )
    session.flush()


def row(session: Session, code: str) -> dict[str, object] | None:
    return backup_alerts._row(session, backup_alerts.ALERT_PREFIX + code)


def audit_actions(session: Session) -> list[str]:
    return list(session.scalars(select(AuditLog.action).order_by(AuditLog.id)))


def test_a_healthy_system_raises_nothing(session: Session) -> None:
    source(session, "fine", "healthy")
    source(session, "wobbly", "degraded")
    result = health_alerts.check(session, NOW)
    assert (result.opened, result.resolved, result.still_open) == ([], [], [])
    assert audit_actions(session) == []


def test_failing_sources_open_one_alert_and_resolve_when_they_recover(session: Session) -> None:
    down = source(session, "punch-rss", "failing")
    source(session, "retired", "failing", active=False)  # not collected, so not an alert
    result = health_alerts.check(session, NOW)
    assert result.opened == [health_alerts.SOURCES_FAILING]
    found = row(session, health_alerts.SOURCES_FAILING)
    assert found is not None and found["state"] == "open"
    assert "punch-rss" in str(found["summary"]) and "retired" not in str(found["summary"])
    assert audit_actions(session) == ["alert.opened"]

    again = health_alerts.check(session, NOW + timedelta(minutes=15))
    assert again.opened == [] and again.still_open == [health_alerts.SOURCES_FAILING]
    assert audit_actions(session) == ["alert.opened"]  # not repeated

    down.health = "healthy"
    session.flush()
    last = health_alerts.check(session, NOW + timedelta(minutes=30))
    assert last.resolved == [health_alerts.SOURCES_FAILING]
    found = row(session, health_alerts.SOURCES_FAILING)
    assert found is not None and found["state"] == "resolved"
    assert audit_actions(session) == ["alert.opened", "alert.resolved"]


def test_dead_jobs_in_the_last_day_open_an_alert_without_their_payload(session: Session) -> None:
    dead_job(session, "fetch_source", NOW - timedelta(hours=2))
    dead_job(session, "fetch_source", NOW - timedelta(hours=3))
    dead_job(session, "extract_claims", NOW - timedelta(hours=1))
    dead_job(session, "old_kind", NOW - timedelta(days=3))  # too old
    dead_job(session, "failed_kind", NOW - timedelta(hours=1), status="failed")  # will retry
    result = health_alerts.check(session, NOW)
    assert result.opened == [health_alerts.JOBS_DEAD]
    found = row(session, health_alerts.JOBS_DEAD)
    assert found is not None
    assert found["detail"] == {
        "by_kind": {"extract_claims": 1, "fetch_source": 2},
        "window_hours": 24,
    }
    assert "3 jobs died" in str(found["summary"])
    assert "reader@example.org" not in str(found)

    later = health_alerts.check(session, NOW + timedelta(days=2))  # they aged out
    assert later.resolved == [health_alerts.JOBS_DEAD]


def test_a_retried_dead_job_clears_the_alert(session: Session) -> None:
    job = dead_job(session, "fetch_source", NOW - timedelta(hours=1))
    health_alerts.check(session, NOW)
    job.status = "queued"
    session.flush()
    assert health_alerts.check(session, NOW).resolved == [health_alerts.JOBS_DEAD]


def test_the_model_budget_alert_starts_at_eighty_percent(session: Session) -> None:
    spend(session, "7.90")  # of the default 10
    assert health_alerts.check(session, NOW).opened == []
    spend(session, "0.20")  # 8.10
    result = health_alerts.check(session, NOW)
    assert result.opened == [health_alerts.LLM_BUDGET_80]
    found = row(session, health_alerts.LLM_BUDGET_80)
    assert found is not None and "$8.10 of $10.00" in str(found["summary"])


def test_raising_the_limit_resolves_the_budget_alert(session: Session) -> None:
    spend(session, "9.00")
    health_alerts.check(session, NOW)
    operator = make_operator(session).operator
    settings_store.apply_changes(session, operator, {"llm_daily_budget_usd": "50"})
    assert health_alerts.check(session, NOW).resolved == [health_alerts.LLM_BUDGET_80]


def test_the_backup_check_leaves_these_alerts_alone(session: Session) -> None:
    source(session, "punch-rss", "failing")
    health_alerts.check(session, NOW)
    backup_alerts.check(session, NOW)  # would resolve any alert it owns whose finding is gone
    found = row(session, health_alerts.SOURCES_FAILING)
    assert found is not None and found["state"] == "open"


def test_the_job_runs_the_check_and_the_scheduler_queues_it(session: Session) -> None:
    source(session, "punch-rss", "failing")
    handlers.load_all()
    handler = handlers.get_handler("check_health")
    assert handler is not None
    job = ClaimedJob(id=1, kind="check_health", payload={}, attempts=1, max_attempts=5)
    handler(JobContext(session=session, job=job, worker_id="test"))
    found = row(session, health_alerts.SOURCES_FAILING)
    assert found is not None and found["state"] == "open"


def test_scheduler_stall_and_oldest_due_job_open_then_resolve_incidents(session: Session) -> None:
    session.add(
        Setting(
            key=health_alerts.SCHEDULER_TICK_KEY,
            value={"last_tick_at": (NOW - timedelta(minutes=7)).isoformat()},
        )
    )
    stalled = Job(
        kind="fetch_source",
        payload={"source_id": 1},
        status="queued",
        created_at=NOW - timedelta(minutes=8),
        run_at=NOW - timedelta(minutes=8),
    )
    session.add(stalled)
    session.flush()

    opened = health_alerts.check(session, NOW)

    assert opened.opened == [health_alerts.SCHEDULER_STALLED, health_alerts.QUEUE_LAG]
    assert row(session, health_alerts.QUEUE_LAG)["detail"]["job_id"] == stalled.id  # type: ignore[index]

    session.get(Setting, health_alerts.SCHEDULER_TICK_KEY).value = {  # type: ignore[union-attr]
        "last_tick_at": NOW.isoformat()
    }
    stalled.status = "running"
    session.flush()
    resolved = health_alerts.check(session, NOW + timedelta(minutes=1))
    assert resolved.resolved == [health_alerts.SCHEDULER_STALLED, health_alerts.QUEUE_LAG]


def test_monthly_series_without_reference_data_opens_a_freshness_incident(
    session: Session,
) -> None:
    source_row = source(session, "monthly-source", "healthy")
    session.add(
        Series(
            item_code="pms_litre",
            topic="energy",
            source_id=source_row.id,
            unit="NGN/litre",
            currency="NGN",
            frequency="monthly",
        )
    )
    session.flush()

    result = health_alerts.check(session, NOW)

    assert result.opened == [health_alerts.SERIES_REFERENCE_STALE]
    found = row(session, health_alerts.SERIES_REFERENCE_STALE)
    assert found is not None
    assert found["detail"]["series"][0]["latest_period_end"] is None  # type: ignore[index]


def test_due_email_backlog_opens_a_delivery_lag_incident(session: Session) -> None:
    session.add(
        Outbox(
            kind="email_digest",
            payload={"user_id": 1},
            dedupe_key="health-alert-delivery-lag",
            status="pending",
            created_at=NOW - timedelta(minutes=15),
            next_attempt_at=NOW - timedelta(minutes=12),
        )
    )
    session.flush()

    result = health_alerts.check(session, NOW)

    assert result.opened == [health_alerts.DELIVERY_LAG]


def test_unknown_email_outcome_opens_a_reconciliation_incident(session: Session) -> None:
    session.add(
        Outbox(
            kind="email_digest",
            payload={"user_id": 1},
            dedupe_key="health-alert-email-unknown",
            status="outcome_unknown",
            attempt_started_at=NOW - timedelta(minutes=6),
        )
    )
    session.flush()

    result = health_alerts.check(session, NOW)

    assert result.opened == [health_alerts.EMAIL_OUTCOME_UNKNOWN]
    found = row(session, health_alerts.EMAIL_OUTCOME_UNKNOWN)
    assert found is not None
    assert "do not blindly retry" in found["summary"]


def test_storage_probe_records_a_safe_incident_and_resolves_on_recovery(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "probe_storage", lambda _session: None)
    healthy = health_alerts.check_storage_probe(session, NOW)
    assert healthy["available"] is True
    assert health_alerts.check(session, NOW).opened == []

    def unavailable(_session: Session) -> None:
        raise PermissionError("secret endpoint response must not be recorded")

    monkeypatch.setattr(storage, "probe_storage", unavailable)
    failed = health_alerts.check_storage_probe(session, NOW + timedelta(minutes=5))
    assert failed["error_type"] == "PermissionError"
    assert "secret endpoint" not in str(failed)
    assert health_alerts.check(session, NOW + timedelta(minutes=5)).opened == [
        health_alerts.STORAGE_UNAVAILABLE
    ]

    monkeypatch.setattr(storage, "probe_storage", lambda _session: None)
    health_alerts.check_storage_probe(session, NOW + timedelta(minutes=10))
    assert health_alerts.check(session, NOW + timedelta(minutes=10)).resolved == [
        health_alerts.STORAGE_UNAVAILABLE
    ]


def test_guardian_schedules_one_delayed_retry_for_permitted_transient_fetch_failure(
    session: Session,
) -> None:
    source_row = source(session, "recoverable", "failing")
    source_row.created_at = NOW - timedelta(days=2)
    session.add(
        SourcePermission(
            source_id=source_row.id,
            version=1,
            may_collect=True,
            may_store_full_text=False,
            may_republish_numbers=False,
            terms_url="https://example.test/terms",
            rights_basis="approved collection access",
            approved_at=NOW - timedelta(days=2),
        )
    )
    failed = Job(
        kind="fetch_source",
        payload={"source_id": source_row.id},
        status="dead",
        attempts=5,
        max_attempts=5,
        last_error="FeedError: Request timed out: connection deadline",
        created_at=NOW - timedelta(hours=1),
        finished_at=NOW - timedelta(minutes=10),
    )
    session.add(failed)
    session.flush()

    from africasignal.guardian.remediation import check_and_repair

    assert check_and_repair(session, NOW) == 1
    attempt = session.scalar(select(GuardianRepairAttempt))
    assert attempt is not None and attempt.state == "queued"
    retry = session.get(Job, attempt.retry_job_id)
    assert retry is not None
    assert retry.kind == "fetch_source" and retry.payload == {"source_id": source_row.id}
    assert retry.run_at == NOW + timedelta(minutes=15)
    assert retry.max_attempts == 1
    assert check_and_repair(session, NOW + timedelta(minutes=1)) == 0
    assert len(list(session.scalars(select(GuardianRepairAttempt)))) == 1


def test_guardian_does_not_repair_denied_or_nontransient_collection_failures(
    session: Session,
) -> None:
    denied = source(session, "denied", "failing")
    denied.created_at = NOW - timedelta(days=2)
    session.add(
        SourcePermission(
            source_id=denied.id,
            version=1,
            may_collect=False,
            may_store_full_text=False,
            may_republish_numbers=False,
            terms_url="https://example.test/terms",
            rights_basis="approved restriction",
            approved_at=NOW - timedelta(days=2),
        )
    )
    parser = source(session, "parser-failure", "failing")
    parser.created_at = NOW - timedelta(days=2)
    session.add(
        SourcePermission(
            source_id=parser.id,
            version=1,
            may_collect=True,
            may_store_full_text=False,
            may_republish_numbers=False,
            terms_url="https://example.test/terms",
            rights_basis="approved collection access",
            approved_at=NOW - timedelta(days=2),
        )
    )
    for source_row, error in (
        (denied, "FeedError: Request timed out: connection deadline"),
        (parser, "FeedError: the response is not an RSS or Atom feed"),
    ):
        session.add(
            Job(
                kind="fetch_source",
                payload={"source_id": source_row.id},
                status="dead",
                attempts=5,
                max_attempts=5,
                last_error=error,
                created_at=NOW - timedelta(hours=1),
                finished_at=NOW - timedelta(minutes=10),
            )
        )
    session.flush()

    from africasignal.guardian.remediation import check_and_repair

    assert check_and_repair(session, NOW) == 0
    assert session.scalar(select(GuardianRepairAttempt.id)) is None
