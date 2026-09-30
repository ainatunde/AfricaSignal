"""Which permission is in force for a source."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import SourcePermission


def current_permission(session: Session, source_id: int) -> SourcePermission | None:
    """The newest permission version with ``approved_at`` set, or None when none is approved."""
    return session.scalars(
        select(SourcePermission)
        .where(SourcePermission.source_id == source_id, SourcePermission.approved_at.is_not(None))
        .order_by(SourcePermission.version.desc())
        .limit(1)
    ).first()
