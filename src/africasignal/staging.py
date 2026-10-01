"""Fail-safe staging initialization.

Every deployment run forces the publication kill switch on before web, worker or scheduler starts.
The operation is intentionally idempotent; staging publication must be resumed manually after
acceptance, and staging plans should normally keep it suspended.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from africasignal.config import get_settings
from africasignal.db import session_scope
from africasignal.publish.versions import set_publication_suspended


def suspend_publication(session: Session | None = None) -> None:
    if get_settings().env != "staging":
        raise RuntimeError("staging initialization requires ENV=staging")
    if session is not None:
        set_publication_suspended(session, True, datetime.now(UTC))
        return
    with session_scope() as managed:
        set_publication_suspended(managed, True, datetime.now(UTC))


def main() -> None:
    suspend_publication()


if __name__ == "__main__":
    main()
