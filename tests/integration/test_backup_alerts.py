"""Backup and restore-drill alerts (AS-041): the ``check_backups`` job and ``backup_alerts.check``."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from africasignal import backup_alerts as alerts
from africasignal import settings_store
from africasignal.jobs import handlers
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.queue import ClaimedJob
from africasignal.models import AuditLog, Setting

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean(session: Session) -> None:
    session.execute(delete(AuditLog))
    session.execute(delete(Setting).where(Setting.key.like("ops.%")))
    session.flush()


def put(session: Session, key: str, value: dict[str, Any]) -> None:
    alerts._write(session, key, value)
    session.flush()


def backup_ok(session: Session, at: datetime) -> None:
    put(session, alerts.BACKUP_STATUS_KEY, {"last_success_at": at.strftime("%Y-%m-%dT%H:%M:%SZ")})


def audit_rows(session: Session) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).order_by(AuditLog.id)))


def alert_row(session: Session, code: str) -> dict[str, Any] | None:
    return alerts._row(session, alerts.ALERT_PREFIX + code)


def production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts, "get_settings", lambda: SimpleNamespace(env="production"))


def test_a_recent_backup_raises_nothing(session: Session) -> None:
    backup_ok(session, NOW - timedelta(hours=26))
    result = alerts.check(session, NOW)
    assert (result.opened, result.resolved, result.still_open) == ([], [], [])
    assert audit_rows(session) == []
    assert alert_row(session, alerts.BACKUP_STALE) is None


def test_a_stale_backup_opens_one_alert_recorded_in_the_audit_log(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    put(
        session,
        alerts.BACKUP_STATUS_KEY,
        {
            "last_success_at": (NOW - timedelta(hours=40)).isoformat(),
            "last_failure_at": (NOW - timedelta(hours=2)).isoformat(),
            "last_failure_reason": "uploaded size 1 does not match local size 2",
        },
    )
    with caplog.at_level(logging.ERROR, logger="africasignal.backup_alerts"):
        result = alerts.check(session, NOW)
    assert result.opened == [alerts.BACKUP_STALE]
    assert "40.0 hours ago" in caplog.text

    row = alert_row(session, alerts.BACKUP_STALE)
    assert row is not None and row["state"] == "open" and row["opened_at"] == NOW.isoformat()
    assert row["detail"]["last_failure_reason"].startswith("uploaded size")

    [entry] = audit_rows(session)
    assert entry.operator_id is None
    assert (entry.action, entry.target_kind) == ("alert.opened", "alert")
    assert entry.after is not None and entry.after["code"] == alerts.BACKUP_STALE


def test_an_open_alert_is_not_audited_again_but_keeps_being_logged(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    backup_ok(session, NOW - timedelta(hours=40))
    alerts.check(session, NOW)
    with caplog.at_level(logging.ERROR, logger="africasignal.backup_alerts"):
        result = alerts.check(session, NOW + timedelta(hours=1))
    assert (result.opened, result.still_open) == ([], [alerts.BACKUP_STALE])
    assert len(audit_rows(session)) == 1
    assert "still open" in caplog.text
    row = alert_row(session, alerts.BACKUP_STALE)
    assert row is not None
    assert row["opened_at"] == NOW.isoformat()
    assert row["last_seen_at"] == (NOW + timedelta(hours=1)).isoformat()
    assert "41.0 hours" in row["summary"]


def test_a_new_backup_resolves_the_alert_and_a_later_lapse_opens_a_new_one(
    session: Session,
) -> None:
    backup_ok(session, NOW - timedelta(hours=40))
    alerts.check(session, NOW)
    backup_ok(session, NOW)
    result = alerts.check(session, NOW + timedelta(minutes=5))
    assert result.resolved == [alerts.BACKUP_STALE]
    row = alert_row(session, alerts.BACKUP_STALE)
    assert row is not None and row["state"] == "resolved"
    assert [a.action for a in audit_rows(session)] == ["alert.opened", "alert.resolved"]

    result = alerts.check(session, NOW + timedelta(hours=50))
    assert result.opened == [alerts.BACKUP_STALE]
    assert [a.action for a in audit_rows(session)][-1] == "alert.opened"


def test_the_age_limit_is_a_console_setting(session: Session) -> None:
    backup_ok(session, NOW - timedelta(hours=10))
    assert alerts.check(session, NOW).opened == []
    settings_store._write_row(session, "backup_max_age_hours", "6", secret=False)
    session.flush()
    assert alerts.check(session, NOW).opened == [alerts.BACKUP_STALE]


def test_no_record_at_all_is_quiet_in_development(session: Session) -> None:
    assert alerts.check(session, NOW).opened == []
    assert alerts.check(session, NOW + timedelta(days=30)).opened == []
    assert alerts._row(session, alerts.WATCH_SINCE_KEY) is None


def test_no_record_at_all_alerts_after_one_limit_outside_development(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    production(monkeypatch)
    assert alerts.check(session, NOW).opened == []  # the clock starts now
    assert alerts.check(session, NOW + timedelta(hours=35)).opened == []
    result = alerts.check(session, NOW + timedelta(hours=37))
    assert result.opened == [alerts.BACKUP_STALE]
    row = alert_row(session, alerts.BACKUP_STALE)
    assert row is not None and "since monitoring began" in row["summary"]
    backup_ok(session, NOW + timedelta(hours=37))
    assert alerts.check(session, NOW + timedelta(hours=38)).resolved == [alerts.BACKUP_STALE]


def test_an_unreadable_timestamp_counts_as_no_success(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    production(monkeypatch)
    put(session, alerts.BACKUP_STATUS_KEY, {"last_success_at": "yesterday-ish"})
    alerts.check(session, NOW)
    assert alerts.check(session, NOW + timedelta(hours=40)).opened == [alerts.BACKUP_STALE]


def test_a_failed_drill_opens_an_alert_and_a_passing_one_resolves_it(session: Session) -> None:
    put(
        session,
        alerts.DRILL_STATUS_KEY,
        {
            "ok": False,
            "mode": "fresh",
            "last_run_at": NOW.isoformat(),
            "detail": "table measurement: source has 12 rows, restored has 11",
            "backup": "africasignal-20261007T120000Z.dump",
        },
    )
    result = alerts.check(session, NOW)
    assert result.opened == [alerts.DRILL_FAILED]
    row = alert_row(session, alerts.DRILL_FAILED)
    assert row is not None and "restored has 11" in row["summary"]

    put(session, alerts.DRILL_STATUS_KEY, {"ok": True, "last_run_at": NOW.isoformat()})
    assert alerts.check(session, NOW + timedelta(hours=1)).resolved == [alerts.DRILL_FAILED]
    assert [a.action for a in audit_rows(session)] == ["alert.opened", "alert.resolved"]


def test_a_drill_that_never_ran_is_not_an_alert(session: Session) -> None:
    backup_ok(session, NOW)
    assert alerts.check(session, NOW).opened == []


def test_the_job_is_registered_and_runs_the_check(session: Session) -> None:
    handlers.load_all()
    handler = handlers.get_handler("check_backups")
    assert handler is not None
    backup_ok(session, datetime.now(UTC) - timedelta(days=3))
    job = ClaimedJob(id=1, kind="check_backups", payload={}, attempts=1, max_attempts=5)
    handler(JobContext(session=session, job=job, worker_id="test"))
    assert alert_row(session, alerts.BACKUP_STALE) is not None
