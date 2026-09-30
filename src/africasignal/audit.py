"""Append a row to ``audit_log`` (spec B3.8). Every operator change calls this in the same
transaction as the change, so a change and its audit row are committed or rolled back together."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from africasignal.models import AuditLog, Operator


def record(
    session: Session,
    operator: Operator,
    action: str,
    target_kind: str,
    target_id: int | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    """``before`` and ``after`` must be JSON-serialisable and must never hold secrets."""
    row = AuditLog(
        operator_id=operator.id,
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        before=before,
        after=after,
    )
    session.add(row)
    session.flush()
    return row
