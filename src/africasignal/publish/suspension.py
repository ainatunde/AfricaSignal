"""The publication kill switch (``setting.publication_suspended``, plan B12)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import Setting


def publication_suspended(session: Session) -> bool:
    """True while the operator has suspended publication. No row means not suspended."""
    value = session.scalar(select(Setting.value).where(Setting.key == "publication_suspended"))
    return value is True
