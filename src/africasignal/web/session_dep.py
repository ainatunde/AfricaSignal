"""The database session for public routes: one per request, read-only, never committed."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from africasignal.db import _session_factory


def get_db() -> Iterator[Session]:
    session = _session_factory()()
    try:
        yield session
    finally:
        session.rollback()  # public pages only read; anything pending is discarded
        session.close()
