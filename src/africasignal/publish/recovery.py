"""Restored databases cannot serve production traffic before deletion replay succeeds."""

from sqlalchemy import select

from africasignal.config import get_settings
from africasignal.db import session_scope
from africasignal.models import Setting


def require_recovery_complete() -> None:
    if get_settings().env == "development":
        return
    with session_scope() as session:
        quarantined = session.scalar(
            select(Setting.value).where(Setting.key == "ops.restore_quarantined")
        )
    if quarantined is True:
        raise RuntimeError("Restored database is quarantined; run reapply-deletions before startup")
