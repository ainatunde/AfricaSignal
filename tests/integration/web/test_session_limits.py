"""Reader sessions end after 7 idle days or 30 days in all, and can be ended everywhere
(security review S-17)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from africasignal.models import UserSession
from africasignal.publish import login_tokens
from tests.integration.email_support import add_user
from tests.integration.web.test_web_accounts import sign_in
from tests.integration.web.web_support import ORIGIN

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def opened(session: Session, at: datetime = T0, email: str = "ada@example.com") -> tuple[int, str]:
    user = add_user(session, email)
    signed = login_tokens._open_session(session, user.id, at)
    return user.id, signed.session_token


def test_a_session_used_every_few_days_lives_until_the_thirty_day_limit(session: Session) -> None:
    _, token = opened(session)
    day = T0
    for _ in range(4):  # four visits, six days apart: never idle for seven days
        day += timedelta(days=6)
        assert login_tokens.user_for_session(session, token, day) is not None
    assert login_tokens.user_for_session(session, token, T0 + timedelta(days=31)) is None


def test_a_session_unused_for_seven_days_ends(session: Session) -> None:
    _, token = opened(session)
    assert login_tokens.user_for_session(session, token, T0 + timedelta(days=6, hours=23))
    # that visit moved the idle clock on, so measure from a fresh session
    _, other = opened(session, T0 + timedelta(days=100), "bob@example.com")
    assert login_tokens.user_for_session(session, other, T0 + timedelta(days=108)) is None


def test_using_a_session_moves_last_seen_forward_at_most_once_an_hour(session: Session) -> None:
    _, token = opened(session)
    row = session.scalars(select(UserSession)).one()
    login_tokens.user_for_session(session, token, T0 + timedelta(minutes=30))
    session.refresh(row)
    assert row.last_seen_at == T0  # too soon to write again
    login_tokens.user_for_session(session, token, T0 + timedelta(hours=2))
    session.refresh(row)
    assert row.last_seen_at == T0 + timedelta(hours=2)


def test_a_session_from_before_the_column_counts_from_when_it_was_created(
    session: Session,
) -> None:
    _, token = opened(session, datetime.now(UTC) - timedelta(days=8))
    session.execute(update(UserSession).values(last_seen_at=None))
    session.flush()
    session.expire_all()
    row = session.scalars(select(UserSession)).one()
    row.created_at = datetime.now(UTC) - timedelta(days=8)
    session.flush()
    assert login_tokens.user_for_session(session, token, datetime.now(UTC)) is None


def test_signing_out_everywhere_ends_every_session_of_the_account(session: Session) -> None:
    user_id, first = opened(session)
    second = login_tokens._open_session(session, user_id, T0).session_token
    other = add_user(session, "bob@example.com")
    bobs = login_tokens._open_session(session, other.id, T0).session_token
    assert login_tokens.revoke_all_sessions(session, user_id, T0) == 2
    assert login_tokens.user_for_session(session, first, T0) is None
    assert login_tokens.user_for_session(session, second, T0) is None
    assert login_tokens.user_for_session(session, bobs, T0) is not None
    assert login_tokens.revoke_all_sessions(session, user_id, T0) == 0  # already ended


def test_the_account_page_signs_out_everywhere(client: TestClient, session: Session) -> None:
    sign_in(client, session)
    assert "Sign out everywhere" in client.get("/account").text
    other = login_tokens._open_session(
        session, session.scalars(select(UserSession.user_id)).one(), datetime.now(UTC)
    )
    assert other.session_token
    response = client.post("/signout-everywhere", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert all(r is not None for r in session.scalars(select(UserSession.revoked_at)))
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_signing_out_everywhere_when_not_signed_in_does_nothing(client: TestClient) -> None:
    response = client.post("/signout-everywhere", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
