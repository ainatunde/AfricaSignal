"""Email reads its provider, key, sender and public address from the settings store on every use,
so a change saved in the operator console applies to the next send without a restart."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.orm import Session

from africasignal import settings_store as ss
from africasignal.publish import email, email_render
from africasignal.publish.email import EmailMessage, PostmarkProvider, ResendProvider
from africasignal.publish.login_tokens import request_login
from africasignal.publish.outbox import dispatch_pending
from tests.integration.email_support import NOW
from tests.integration.test_admin_console import make_operator


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("email_provider", "email_api_key", "email_from", "public_base_url"):
        monkeypatch.delenv(key.upper(), raising=False)
    email.set_provider(None)


def test_a_saved_provider_applies_on_the_next_call(session: Session) -> None:
    operator = make_operator(session).operator
    assert isinstance(email.get_provider(session), email.ConsoleProvider)  # development default

    ss.set_value(session, operator, "email_provider", "postmark")
    ss.set_value(session, operator, "email_api_key", "postmark-key-123")
    ss.set_value(session, operator, "email_from", "AfricaSignal <hello@africasignal.example>")
    provider = email.get_provider(session)
    assert isinstance(provider, PostmarkProvider)

    ss.set_value(session, operator, "email_provider", "resend")
    assert isinstance(email.get_provider(session), ResendProvider)


def test_the_saved_key_and_sender_reach_the_request(session: Session) -> None:
    operator = make_operator(session).operator
    ss.set_value(session, operator, "email_provider", "resend")
    ss.set_value(session, operator, "email_api_key", "first-key-123")
    ss.set_value(session, operator, "email_from", "hello@africasignal.example")
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "re-1"})

    message = EmailMessage(to="a@example.com", subject="s", text="t")
    for key in ("first-key-123", "second-key-456"):
        ss.set_value(session, operator, "email_api_key", key)
        provider = email.get_provider(session)
        assert isinstance(provider, ResendProvider)
        provider._client = httpx.Client(transport=httpx.MockTransport(handle))
        provider.send(message)

    assert [r.headers["Authorization"] for r in seen] == [
        "Bearer first-key-123",
        "Bearer second-key-456",
    ]


def test_a_saved_value_wins_over_the_environment(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = make_operator(session).operator
    monkeypatch.setenv("EMAIL_PROVIDER", "console")
    assert isinstance(email.get_provider(session), email.ConsoleProvider)
    ss.set_value(session, operator, "email_provider", "resend")
    ss.set_value(session, operator, "email_api_key", "a-saved-key-1")
    ss.set_value(session, operator, "email_from", "hello@africasignal.example")
    assert isinstance(email.get_provider(session), ResendProvider)
    ss.clear_value(session, operator, "email_provider")  # the environment applies again
    assert isinstance(email.get_provider(session), email.ConsoleProvider)


def test_ses_is_refused_with_a_clear_error(session: Session) -> None:
    operator = make_operator(session).operator
    ss.set_value(session, operator, "email_provider", "ses")
    with pytest.raises(RuntimeError, match="not implemented"):
        email.get_provider(session)


def test_the_public_address_is_read_when_links_are_built(session: Session) -> None:
    operator = make_operator(session).operator
    assert email_render.base_url(session) == email_render.DEV_BASE_URL

    ss.set_value(session, operator, "public_base_url", "https://africasignal.example/")
    assert request_login(session, "a@example.com", NOW)
    provider = email.FakeProvider()
    dispatch_pending(session, provider, NOW)
    assert "https://africasignal.example/signin/verify?token=" in provider.sent[0].text

    ss.set_value(session, operator, "public_base_url", "https://other.example")
    assert email_render.base_url(session) == "https://other.example"


def test_outside_development_a_missing_public_address_is_an_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from africasignal.config import get_settings

    monkeypatch.setattr(get_settings(), "env", "production")
    with pytest.raises(RuntimeError, match="public address"):
        email_render.base_url(session)
