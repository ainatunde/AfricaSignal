"""Backup alerts page: the ``ops.alert.*`` records the hourly ``check_backups`` job keeps (AS-041),
with the backup and restore-drill status they are judged from. Read only: an alert resolves
itself when the check next finds nothing wrong."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import backup_alerts
from africasignal.models import AuditLog, Setting


@dataclass(frozen=True)
class Alert:
    code: str
    state: str
    opened_at: datetime | None
    last_seen_at: datetime | None
    resolved_at: datetime | None
    summary: str
    detail: dict[str, Any]

    @property
    def is_open(self) -> bool:
        return self.state == "open"


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def alerts(session: Session) -> list[Alert]:
    """Every ``ops.alert.*`` row, open ones first, then newest first."""
    rows = session.execute(
        select(Setting.key, Setting.value).where(Setting.key.like(backup_alerts.ALERT_PREFIX + "%"))
    )
    found: list[Alert] = []
    for key, value in rows:
        if not isinstance(value, dict):
            continue
        detail = value.get("detail")
        found.append(
            Alert(
                code=key.removeprefix(backup_alerts.ALERT_PREFIX),
                state=str(value.get("state", "unknown")),
                opened_at=_when(value.get("opened_at")),
                last_seen_at=_when(value.get("last_seen_at")),
                resolved_at=_when(value.get("resolved_at")),
                summary=str(value.get("summary", "")),
                detail=dict(detail) if isinstance(detail, dict) else {},
            )
        )
    found.sort(key=lambda a: (not a.is_open, -(a.opened_at.timestamp() if a.opened_at else 0)))
    return found


def status_record(session: Session, key: str) -> dict[str, Any] | None:
    value = session.scalar(select(Setting.value).where(Setting.key == key))
    return dict(value) if isinstance(value, dict) else None


def history(session: Session, limit: int = 30) -> list[AuditLog]:
    """The system's alert.opened and alert.resolved audit rows, newest first."""
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.action.in_(("alert.opened", "alert.resolved")))
            .order_by(AuditLog.id.desc())
            .limit(limit)
        )
    )
