"""Magic-link sign-in (AS-031): tokens, sessions and the email that carries them."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal.models import AppUser, LoginToken, Outbox, UserSession
from africasignal.publish.email import FakeProvider
from africasignal.publish.login_tokens import (
    LOGIN_TOKEN_TTL,
    MAX_LOGIN_REQUESTS_PER_HOUR,
    consume_login_token,
    normalise_email,
    request_login,
    revoke_session,
    user_for_session,
)
from africasignal.publish.outbox import dispatch_pending
from tests.integration.email_support import NOW


def sign_in_link_token(provider: FakeProvider) -> str:
    link = next(line for line in provider.sent[-1].text.splitlines() if line.startswith("http"))
    return parse_qs(urlparse(link).query)["token"][0]


def test_normalise_email() -> None:
    assert normalise_email("  Ada@Example.COM ") == "ada@example.com"
    for bad in ["", "no-at-sign", "a@b", "a b@c.de", "@c.de", "a@" + "b" * 260 + ".com"]:
        assert normalise_email(bad) is None


def test_full_sign_in_flow(session: Session) -> None:
    assert request_login(session, "Ada@Example.com", NOW)
    provider = FakeProvider()
    assert dispatch_pending(session, provider, NOW).sent == 1
    assert provider.sent[0].to == "ada@example.com"

    user = session.scalars(select(AppUser)).one()
    assert user.email_verified_at is None  # not verified until the link is used

    signed_in = consume_login_token(session, sign_in_link_token(provider), NOW)
    assert signed_in is not None and signed_in.user_id == user.id
    assert user.email_verified_at == NOW
    assert signed_in.expires_at == NOW + timedelta(days=30)

    # Used every few days, so the 7-day idle limit (S-17) never ends it before 30 days do.
    for day in (5, 10, 15, 20, 25, 29):
        found = user_for_session(session, signed_in.session_token, NOW + timedelta(days=day))
        assert found is not None and found.id == user.id
    assert user_for_session(session, signed_in.session_token, NOW + timedelta(days=31)) is None
    revoke_session(session, signed_in.session_token, NOW)
    assert user_for_session(session, signed_in.session_token, NOW) is None


def test_tokens_are_stored_hashed_and_work_once(session: Session) -> None:
    request_login(session, "a@example.com", NOW)
    provider = FakeProvider()
    dispatch_pending(session, provider, NOW)
    raw = sign_in_link_token(provider)

    stored = session.scalars(select(LoginToken)).one()
    assert raw not in stored.token_sha256 and len(stored.token_sha256) == 64

    assert consume_login_token(session, raw, NOW) is not None
    assert consume_login_token(session, raw, NOW) is None  # single use
    session_row = session.scalars(select(UserSession)).one()
    assert session_row.token_sha256 != raw


def test_expired_and_unknown_tokens_fail(session: Session) -> None:
    request_login(session, "a@example.com", NOW)
    provider = FakeProvider()
    dispatch_pending(session, provider, NOW)
    raw = sign_in_link_token(provider)
    assert consume_login_token(session, raw, NOW + LOGIN_TOKEN_TTL + timedelta(seconds=1)) is None
    assert consume_login_token(session, "not-a-token", NOW) is None
    assert user_for_session(session, "not-a-session", NOW) is None


def test_request_login_is_rate_limited_per_address(session: Session) -> None:
    results = [request_login(session, "a@example.com", NOW) for _ in range(8)]
    assert results.count(True) == MAX_LOGIN_REQUESTS_PER_HOUR
    assert session.scalar(select(func.count()).select_from(Outbox)) == MAX_LOGIN_REQUESTS_PER_HOUR
    later = NOW + timedelta(hours=1, minutes=1)
    assert request_login(session, "a@example.com", later)


@pytest.mark.parametrize("bad", ["", "nonsense"])
def test_bad_address_queues_nothing(session: Session, bad: str) -> None:
    assert not request_login(session, bad, NOW)
    assert session.scalar(select(func.count()).select_from(Outbox)) == 0
    assert session.scalar(select(func.count()).select_from(AppUser)) == 0


def test_the_same_address_keeps_one_account(session: Session) -> None:
    request_login(session, "a@example.com", NOW)
    request_login(session, "A@EXAMPLE.COM", NOW)
    assert session.scalar(select(func.count()).select_from(AppUser)) == 1
