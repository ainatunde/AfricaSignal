from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import Setting
from africasignal.staging import suspend_publication


def test_staging_initialization_is_idempotently_suspended(session: Session, monkeypatch) -> None:
    from africasignal import staging
    from africasignal.config import Settings

    monkeypatch.setattr(staging, "get_settings", lambda: Settings(ENV="staging"))
    session.add(Setting(key="publication_suspended", value=False))
    session.flush()

    suspend_publication(session)
    suspend_publication(session)

    assert (
        session.scalar(select(Setting.value).where(Setting.key == "publication_suspended")) is True
    )


def test_staging_initialization_refuses_other_environments(monkeypatch) -> None:
    import pytest

    from africasignal import staging
    from africasignal.config import Settings

    monkeypatch.setattr(staging, "get_settings", lambda: Settings(ENV="production"))
    with pytest.raises(RuntimeError, match="ENV=staging"):
        staging.suspend_publication()
