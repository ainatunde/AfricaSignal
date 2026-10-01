"""Alerts on backups and restore drills (AS-041).

``scripts/backup.sh`` and ``scripts/restore-drill.sh`` leave a small JSON record in the ``setting``
table: ``ops.backup_status`` (``last_success_at``, ``last_failure_at``, ``last_failure_reason``) and
``ops.restore_drill_status`` (``last_run_at``, ``ok``, ``detail``, ...). The scheduler's hourly
``check_backups`` job calls :func:`check`, which raises two alerts:

- ``backup_stale``: the last successful backup is older than ``backup_max_age_hours`` (console
  setting, default 36), or, outside development, none has been recorded since this job first ran.
- ``restore_drill_failed``: the last restore drill did not pass; a later passing drill resolves it.

An alert is the ``ops.alert.<code>`` row in ``setting`` (``state`` open or resolved, when it opened,
when it was last seen, a summary and details), an ``alert.opened`` / ``alert.resolved`` row in the
audit log written by the system (not an operator) when its state changes, and an ERROR log line on
every check while it stays open. Nothing is sent anywhere: there is no operator notification channel
yet, so the audit log, the ``ops.alert.*`` rows and the log are where an alert is seen. The console
page that lists open alerts reads ``ops.alert.*``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal import audit, settings_store
from africasignal.config import get_settings
from africasignal.models import Setting

log = logging.getLogger("africasignal.backup_alerts")

BACKUP_STATUS_KEY = "ops.backup_status"
DRILL_STATUS_KEY = "ops.restore_drill_status"
WATCH_SINCE_KEY = "ops.backup_watch_since"
ALERT_PREFIX = "ops.alert."

BACKUP_STALE = "backup_stale"
DRILL_FAILED = "restore_drill_failed"
ALERT_CODES = (BACKUP_STALE, DRILL_FAILED)


@dataclass(frozen=True)
class Finding:
    code: str
    summary: str
    detail: dict[str, Any]


@dataclass
class CheckResult:
    opened: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    still_open: list[str] = field(default_factory=list)


def _row(session: Session, key: str) -> dict[str, Any] | None:
    # A column query, not session.get(), so a value written earlier in this transaction is seen.
    found = session.execute(select(Setting.value).where(Setting.key == key)).first()
    if found is None or not isinstance(found[0], dict):
        return None
    return dict(found[0])


def _write(session: Session, key: str, value: dict[str, Any]) -> None:
    stmt = pg_insert(Setting).values(key=key, value=value)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Setting.key],
            set_={"value": value, "updated_at": stmt.excluded.updated_at},
        )
    )


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _watch_since(session: Session, now: datetime) -> datetime:
    """When this check first ran; the start of the clock for "no backup has ever been recorded"."""
    session.execute(
        pg_insert(Setting)
        .values(key=WATCH_SINCE_KEY, value={"at": now.isoformat()})
        .on_conflict_do_nothing(index_elements=[Setting.key])
    )
    row = _row(session, WATCH_SINCE_KEY) or {}
    return _parse(row.get("at")) or now


def _hours(delta_seconds: float) -> str:
    return f"{delta_seconds / 3600:.1f}"


def evaluate(session: Session, now: datetime) -> list[Finding]:
    """What is wrong right now. Reads only; ``check`` turns findings into alerts."""
    findings: list[Finding] = []
    limit_hours = settings_store.get_int(session, "backup_max_age_hours") or 36
    status = _row(session, BACKUP_STATUS_KEY) or {}

    last_success = _parse(status.get("last_success_at"))
    context = {
        "max_age_hours": limit_hours,
        "last_success_at": status.get("last_success_at"),
        "last_failure_at": status.get("last_failure_at"),
        "last_failure_reason": status.get("last_failure_reason"),
    }
    if last_success is not None:
        age = (now - last_success).total_seconds()
        if age > limit_hours * 3600:
            findings.append(
                Finding(
                    BACKUP_STALE,
                    f"The last successful backup was {_hours(age)} hours ago "
                    f"(alert after {limit_hours}).",
                    context,
                )
            )
    elif get_settings().env != "development":
        # Nothing ever recorded. In development nobody runs backups, so stay quiet there; anywhere
        # else a backup job that never ran is the worst case, so give it one limit, then alert.
        since = _watch_since(session, now)
        waited = (now - since).total_seconds()
        if waited > limit_hours * 3600:
            findings.append(
                Finding(
                    BACKUP_STALE,
                    f"No successful backup has been recorded in the {_hours(waited)} hours "
                    f"since monitoring began (alert after {limit_hours}).",
                    {**context, "monitoring_since": since.isoformat()},
                )
            )

    drill = _row(session, DRILL_STATUS_KEY) or {}
    if drill.get("ok") is False:
        findings.append(
            Finding(
                DRILL_FAILED,
                f"The last restore drill ({drill.get('mode', 'unknown')} mode, "
                f"{drill.get('last_run_at', 'time unknown')}) failed: "
                f"{drill.get('detail') or 'no reason recorded'}",
                {
                    "last_run_at": drill.get("last_run_at"),
                    "mode": drill.get("mode"),
                    "backup": drill.get("backup"),
                    "detail": drill.get("detail"),
                    "last_success_at": drill.get("last_success_at"),
                },
            )
        )
    return findings


def check(session: Session, now: datetime | None = None) -> CheckResult:
    """Open alerts for new findings, resolve alerts whose finding is gone, and log the ones still
    open. The caller commits."""
    now = now or datetime.now(UTC)
    result = CheckResult()
    findings = {f.code: f for f in evaluate(session, now)}
    stamp = now.isoformat()
    for code in ALERT_CODES:
        key = ALERT_PREFIX + code
        existing = _row(session, key)
        was_open = existing is not None and existing.get("state") == "open"
        finding = findings.get(code)
        if finding is not None and not was_open:
            _write(
                session,
                key,
                {
                    "state": "open",
                    "opened_at": stamp,
                    "last_seen_at": stamp,
                    "summary": finding.summary,
                    "detail": finding.detail,
                },
            )
            audit.record_system(
                session,
                "alert.opened",
                "alert",
                after={"code": code, "summary": finding.summary, **finding.detail},
            )
            log.error("ALERT %s opened: %s", code, finding.summary)
            result.opened.append(code)
        elif finding is not None and existing is not None:
            _write(
                session,
                key,
                {
                    **existing,
                    "last_seen_at": stamp,
                    "summary": finding.summary,
                    "detail": finding.detail,
                },
            )
            log.error(
                "ALERT %s still open since %s: %s", code, existing.get("opened_at"), finding.summary
            )
            result.still_open.append(code)
        elif was_open and existing is not None:
            _write(session, key, {**existing, "state": "resolved", "resolved_at": stamp})
            audit.record_system(
                session,
                "alert.resolved",
                "alert",
                before={"code": code, "opened_at": existing.get("opened_at")},
                after={"code": code, "resolved_at": stamp},
            )
            log.info("alert %s resolved", code)
            result.resolved.append(code)
    return result
