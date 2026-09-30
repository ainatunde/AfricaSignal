# ruff: noqa: F811
"""The publication kill switch in the operator console (AS-043 gap G6, launch check P5)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import AuditLog
from africasignal.publish.suspension import publication_suspended
from africasignal.web import deps
from africasignal.web.app import create_app
from africasignal.web.routes import public
from africasignal.web.session_dep import get_db as public_get_db
from tests.integration.test_admin_console import (  # noqa: F401  (fixtures and helpers)
    ORIGIN,
    _clean_state,
    audit_actions,
    make_operator,
    signed_in,
)

BROWSER = {"User-Agent": "Mozilla/5.0"}


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    """The console and the public site on one app, both using the test's session."""
    app = create_app()
    app.dependency_overrides[deps.get_db] = lambda: session
    app.dependency_overrides[public_get_db] = lambda: session
    public.clear_page_cache()
    with TestClient(app, follow_redirects=False) as c:
        yield c
    public.clear_page_cache()


def switch(client: TestClient, action: str, **kwargs: object):  # type: ignore[no-untyped-def]
    return client.post(f"/admin/publication/{action}", headers=ORIGIN, **kwargs)


def test_the_page_and_the_buttons_are_admin_only(client: TestClient, session: Session) -> None:
    assert client.get("/admin/publication").status_code == 303  # signed out
    assert switch(client, "suspend").status_code == 303
    assert not publication_suspended(session)

    signed_in(client, make_operator(session, "editor@example.org", "editor"))
    assert client.get("/admin/publication").status_code == 403
    assert switch(client, "suspend").status_code == 403
    assert not publication_suspended(session)
    assert "/admin/publication" not in client.get("/admin/sources").text  # no nav link


def test_an_admin_can_suspend_and_resume_and_every_change_is_audited(
    client: TestClient, session: Session
) -> None:
    signed_in(client, make_operator(session))
    assert "/admin/publication" in client.get("/admin/sources").text
    page = client.get("/admin/publication")
    assert "Publication is running" in page.text and "Suspend publication" in page.text
    assert "Updates are paused" not in client.get("/coverage", headers=BROWSER).text

    response = switch(client, "suspend")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/publication?notice=suspended"
    assert publication_suspended(session)
    page = client.get("/admin/publication")
    assert "Publication is suspended" in page.text and "Resume publication" in page.text
    assert "Updates are paused" in client.get("/coverage", headers=BROWSER).text  # the banner

    assert switch(client, "resume").status_code == 303
    assert not publication_suspended(session)
    assert "Updates are paused" not in client.get("/coverage", headers=BROWSER).text

    assert [a for a in audit_actions(session) if a.startswith("publication.")] == [
        "publication.suspend",
        "publication.resume",
    ]
    first = session.scalars(
        select(AuditLog).where(AuditLog.action == "publication.suspend")
    ).first()
    assert first is not None
    assert first.before == {"publication_suspended": False}
    assert first.after == {"publication_suspended": True}


def test_repeating_the_current_state_changes_nothing(client: TestClient, session: Session) -> None:
    signed_in(client, make_operator(session))
    assert switch(client, "resume").headers["location"] == "/admin/publication?notice=unchanged"
    switch(client, "suspend")
    assert switch(client, "suspend").headers["location"] == "/admin/publication?notice=unchanged"
    actions = audit_actions(session)
    assert actions.count("publication.suspend") == 1
    assert "publication.resume" not in actions


def test_an_unknown_action_is_a_404_and_a_cross_site_post_is_refused(
    client: TestClient, session: Session
) -> None:
    signed_in(client, make_operator(session))
    assert switch(client, "pause").status_code == 404
    response = client.post("/admin/publication/suspend", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert not publication_suspended(session)
