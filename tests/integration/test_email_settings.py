"""Email and links read the operator's console settings on every use (AS-031/032 wiring).

The email provider, sender, API key and public address are saved in the console (``settings_store``),
with environment variables as the fallback. A change applies to the next email, with no restart.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import operators, settings_store
from africasignal.config import Settings
from africasignal.models import AppUser, LoginToken, Operator, Outbox
from africasignal.publish import email as email_module
from africasignal.publish import email_render
from africasignal.publish.email import (
    ConsoleProvider,
    EmailNotConfigured,
    FakeProvider,
    PostmarkProvider,
    ResendProvider,
    get_provider,
)
from africasignal.publish.login_tokens import request_login
from africasignal.publish.outbox import (
    dispatch_pending,
    dispatch_with_configured_provider,
    enqueue_email,
)
from tests.integration.email_support import NOW, add_user

ADDRESS = "https://africasignal.example"


@pytest.fixture(autouse=True)
def _fresh_provider() -> Iterator[None]:
    email_module.set_provider(None)
    yield
    email_module.set_provider(None)


@pytest.fixture
def operator(session: Session) -> Operator:
    return operators.create_operator(session, "ops@example.org", "correct horse battery", "admin")[
        0
    ]


@pytest.fixture
def not_development(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behave as staging: nothing is configured unless the console says so."""
    monkeypatch.setattr(email_module, "get_settings", lambda: Settings(ENV="staging"))  # type: ignore[call-arg]
    monkeypatch.setattr(email_render, "get_settings", lambda: Settings(ENV="staging"))  # type: ignore[call-arg]


def save(session: Session, operator: Operator, **values: str) -> None:
    settings_store.apply_changes(session, operator, dict(values))


# --- the provider -------------------------------------------------------------------------------


def test_the_provider_follows_the_console(session: Session, operator: Operator) -> None:
    assert isinstance(get_provider(session), ConsoleProvider)  # development, nothing configured
    save(
        session,
        operator,
        email_provider="resend",
        email_api_key="re_test_key_123456",
        email_from="AfricaSignal <hello@africasignal.example>",
    )
    provider = get_provider(session)
    assert isinstance(provider, ResendProvider) and get_provider(session) is provider
    save(session, operator, email_provider="postmark")
    assert isinstance(get_provider(session), PostmarkProvider)  # the next call, no restart
    save(session, operator, email_api_key="pm_other_key_123456")
    assert get_provider(session) is not provider
    settings_store.clear_value(session, operator, "email_provider")
    assert isinstance(get_provider(session), ConsoleProvider)


def test_an_unusable_choice_leaves_mail_waiting(
    session: Session, operator: Operator, not_development: None
) -> None:
    with pytest.raises(EmailNotConfigured, match="no email provider"):
        get_provider(session)
    save(session, operator, email_provider="ses")
    with pytest.raises(EmailNotConfigured, match="not implemented"):
        get_provider(session)
    save(session, operator, email_provider="postmark")  # no key or sender yet
    with pytest.raises(EmailNotConfigured, match="needs an API key"):
        get_provider(session)


def test_unconfigured_mail_stays_pending_and_nothing_is_dropped(
    session: Session, operator: Operator, not_development: None
) -> None:
    user = add_user(session, "ada@example.com")
    enqueue_email(session, "email_login", {"user_id": user.id, "link": "https://x/y"}, "login:1")
    assert dispatch_with_configured_provider(session, NOW) is None  # logs a warning, sends nothing
    row = session.scalars(select(Outbox)).one()
    assert (row.status, row.attempts) == ("pending", 0) and row.payload["link"]

    save(
        session,
        operator,
        email_provider="resend",
        email_api_key="re_test_key_123456",
        email_from="hello@africasignal.example",
        public_base_url=ADDRESS,
    )
    email_module.set_provider(FakeProvider())
    result = dispatch_with_configured_provider(session, NOW)
    assert result is not None and result.sent == 1
    session.refresh(row)
    assert row.status == "sent"


# --- the public address -------------------------------------------------------------------------


def test_sign_in_links_use_the_address_from_the_console(
    session: Session, operator: Operator
) -> None:
    save(session, operator, public_base_url=ADDRESS)
    assert request_login(session, "ada@example.com", NOW)
    link = session.scalars(select(Outbox)).one().payload["link"]
    assert link.startswith(f"{ADDRESS}/signin/verify?token=")

    save(session, operator, public_base_url="https://other.example")
    assert request_login(session, "bob@example.com", NOW)
    links = [o.payload["link"] for o in session.scalars(select(Outbox).order_by(Outbox.id))]
    assert links[1].startswith("https://other.example/signin/verify?token=")


def test_without_an_address_a_real_environment_sends_no_link(
    session: Session, not_development: None
) -> None:
    """A sign-in link to localhost in a real email is worse than no email."""
    assert request_login(session, "ada@example.com", NOW) is False
    assert session.scalars(select(Outbox)).all() == []
    assert session.scalars(select(LoginToken)).all() == []
    assert session.scalars(select(AppUser.email)).all() == ["ada@example.com"]


def test_development_falls_back_to_localhost(session: Session) -> None:
    assert email_render.resolve_base_url(session) == "http://localhost:8000"


def test_emails_built_at_send_time_use_the_current_address(
    session: Session, operator: Operator
) -> None:
    user = add_user(session, "ada@example.com", digest=True)
    item = {
        "slug": "price-pms",
        "title": "T",
        "headline": "H",
        "scope_label": "L",
        "change_summary": None,
    }
    enqueue_email(
        session,
        "email_correction",
        {"user_id": user.id, "notification_kind": "correction", "item": item},
        "correction:1",
    )
    enqueue_email(
        session,
        "email_digest",
        {"user_id": user.id, "week": "2026-W40", "followed": [item], "top": []},
        "digest:1",
    )
    save(session, operator, public_base_url=ADDRESS)  # saved after the emails were queued
    provider = FakeProvider()
    assert dispatch_pending(session, provider, NOW).sent == 2
    for message in provider.sent:
        links = [w for w in message.text.split() if w.startswith("http")]
        assert links and all(urlparse(link).netloc == "africasignal.example" for link in links)
        assert message.headers["List-Unsubscribe"].startswith(f"<{ADDRESS}/unsubscribe?t=")
    assert f'src="{ADDRESS}/e/o/' in (provider.sent[1].html or "")
