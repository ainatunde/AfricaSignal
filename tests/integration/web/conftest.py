"""Fixtures for the public site and API tests: real NBS data in an in-memory object store, and a
test client that uses the test's own database session."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from africasignal.models import Source
from africasignal.storage import S3Store
from africasignal.web.app import create_app
from africasignal.web.routes import account, api_v1, feedback, public
from africasignal.web.session_dep import get_db
from tests.integration.nbs_support import add_places, add_source, make_store


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from make_store()


@pytest.fixture
def places(session: Session) -> dict[str, int]:
    return add_places(session)


@pytest.fixture
def source(session: Session, places: dict[str, int]) -> Source:
    return add_source(session)


@contextmanager
def _events(session: Session) -> Iterator[Session]:
    yield session


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app = create_app()

    def override() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = override
    app.state.event_session = lambda: _events(session)  # page views land in the test's session
    public.clear_page_cache()
    api_v1.rate_limiter.reset()
    account.signin_limiter.reset()
    feedback.share_limiter.reset()
    feedback.feedback_limiter.reset()
    public.locate_limiter.reset()
    with TestClient(app) as test_client:
        yield test_client
    public.clear_page_cache()
