"""The provider adapters against a mocked HTTP transport: no network."""

from __future__ import annotations

import json

import httpx
import pytest

from africasignal.config import Settings
from africasignal.publish.email import (
    ConsoleProvider,
    EmailMessage,
    EmailSendError,
    FakeProvider,
    PostmarkProvider,
    ResendProvider,
    build_provider,
)

MESSAGE = EmailMessage(
    to="ada@example.com",
    subject="Hello",
    text="plain",
    html="<p>html</p>",
    headers={"List-Unsubscribe": "<https://x/u>"},
    idempotency_key="digest:1:2026-W40",
)


def client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_postmark_sends_expected_request() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ErrorCode": 0, "MessageID": "pm-1"})

    provider = PostmarkProvider(
        "tok", "AfricaSignal <hello@example.org>", client(httpx.MockTransport(handle))
    )
    assert provider.send(MESSAGE) == "pm-1"
    request = seen[0]
    assert request.headers["X-Postmark-Server-Token"] == "tok"
    body = json.loads(request.content)
    assert body["To"] == "ada@example.com"
    assert body["TextBody"] == "plain" and body["HtmlBody"] == "<p>html</p>"
    assert {"Name": "List-Unsubscribe", "Value": "<https://x/u>"} in body["Headers"]


def test_resend_sends_idempotency_key() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "re-1"})

    provider = ResendProvider("key", "hello@example.org", client(httpx.MockTransport(handle)))
    assert provider.send(MESSAGE) == "re-1"
    assert seen[0].headers["Idempotency-Key"] == "digest:1:2026-W40"
    assert seen[0].headers["Authorization"] == "Bearer key"
    assert json.loads(seen[0].content)["to"] == ["ada@example.com"]


@pytest.mark.parametrize(
    ("status", "retryable"), [(429, True), (500, True), (503, True), (400, False), (422, False)]
)
def test_http_errors_are_classified(status: int, retryable: bool) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text="nope"))
    with pytest.raises(EmailSendError) as info:
        ResendProvider("k", "a@b.co", client(transport)).send(MESSAGE)
    assert info.value.retryable is retryable


def test_connection_errors_are_retryable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(EmailSendError) as info:
        ResendProvider("k", "a@b.co", client(httpx.MockTransport(handle))).send(MESSAGE)
    assert info.value.retryable


def test_postmark_rejection_with_http_200_is_permanent() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ErrorCode": 406, "Message": "inactive address"})
    )
    with pytest.raises(EmailSendError) as info:
        PostmarkProvider("k", "a@b.co", client(transport)).send(MESSAGE)
    assert not info.value.retryable


def test_fake_provider_records_and_can_fail() -> None:
    fake = FakeProvider()
    assert fake.send(MESSAGE) == "fake-1" and fake.sent == [MESSAGE]
    fake.fail_with = EmailSendError("down")
    with pytest.raises(EmailSendError):
        fake.send(MESSAGE)


def test_build_provider_selects_by_name() -> None:
    def settings(name: str) -> Settings:
        return Settings(EMAIL_PROVIDER=name, EMAIL_API_KEY="k", EMAIL_FROM="a@b.co")  # type: ignore[call-arg]

    assert isinstance(build_provider(settings("")), ConsoleProvider)
    assert isinstance(build_provider(settings("Postmark")), PostmarkProvider)
    assert isinstance(build_provider(settings("resend")), ResendProvider)
    with pytest.raises(RuntimeError):
        build_provider(settings("carrier-pigeon"))


@pytest.mark.parametrize("name", ["", "fake", "console", " Console "])
@pytest.mark.parametrize("env", ["staging", "production"])
def test_non_live_providers_are_refused_outside_development(name: str, env: str) -> None:
    from africasignal.publish.email import EmailNotConfigured

    settings = Settings(ENV=env, EMAIL_PROVIDER=name)  # type: ignore[call-arg]
    with pytest.raises(EmailNotConfigured):
        build_provider(settings)


@pytest.mark.parametrize(
    "provider_type,key", [(ResendProvider, "id"), (PostmarkProvider, "MessageID")]
)
@pytest.mark.parametrize("value", [None, "", " ", 123])
def test_success_without_message_id_is_uncertain(provider_type, key, value) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={key: value}))
    with pytest.raises(EmailSendError, match="outcome uncertain"):
        provider_type("key", "a@b.co", client(transport)).send(MESSAGE)


@pytest.mark.parametrize(
    "name,retryable",
    [("concurrent_idempotent_requests", True), ("invalid_idempotent_request", False)],
)
def test_resend_conflicts_distinguish_concurrent_retries(name: str, retryable: bool) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(409, json={"name": name}))
    with pytest.raises(EmailSendError) as exc:
        ResendProvider("key", "a@b.co", client(transport)).send(MESSAGE)
    assert exc.value.retryable is retryable
