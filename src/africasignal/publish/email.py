"""Email provider adapter (plan D6).

The provider is not chosen yet, so everything that sends mail depends only on
:class:`EmailProvider`. ``EMAIL_PROVIDER`` selects the implementation:

- ``console``: logs the message instead of sending it (development default)
- ``fake``: keeps messages in memory (tests)
- ``postmark``, ``resend``: HTTP APIs, through ``httpx``

The Postmark and Resend adapters follow the providers' public API documentation and are tested
against a mocked transport only; none has been run against the live service. Amazon SES is not
implemented. To add a provider, write a class with ``send`` and register it in ``PROVIDERS``.

``EmailMessage.idempotency_key`` is the outbox ``dedupe_key``. Providers that accept an
idempotency key get it, so a retry after a timeout does not send twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from africasignal.config import Settings, get_settings

log = logging.getLogger("africasignal.email")

SEND_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    idempotency_key: str | None = None


class EmailSendError(Exception):
    """A send failed. ``retryable`` is False when trying again cannot help (bad address,
    rejected content, bad credentials); the outbox then marks the row ``dead`` at once."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class EmailProvider(Protocol):
    def send(self, message: EmailMessage) -> str:
        """Send one message and return the provider's message id."""
        ...


class ConsoleProvider:
    """Logs each message. The body is not logged: it can hold a sign-in link."""

    def send(self, message: EmailMessage) -> str:
        log.info("email not sent (console provider): subject=%r", message.subject)
        return f"console-{message.idempotency_key or 'none'}"


class FakeProvider:
    """Records messages in memory; ``fail_with`` makes every send raise that error."""

    def __init__(self, fail_with: EmailSendError | None = None) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_with = fail_with

    def send(self, message: EmailMessage) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)
        return f"fake-{len(self.sent)}"


def _classify(response: httpx.Response) -> EmailSendError | None:
    """Turn an HTTP response into an error, or ``None`` for success. 429 and 5xx can be retried;
    any other 4xx cannot."""
    if response.is_success:
        return None
    retryable = response.status_code == 429 or response.status_code >= 500
    return EmailSendError(
        f"HTTP {response.status_code}: {response.text[:200]}", retryable=retryable
    )


class _HttpProvider:
    def __init__(self, api_key: str, from_address: str, client: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._from = from_address
        self._client = client or httpx.Client(timeout=SEND_TIMEOUT_SECONDS)

    def _post(self, url: str, headers: dict[str, str], body: dict[str, object]) -> dict[str, str]:
        try:
            response = self._client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:  # timeouts, connection errors
            raise EmailSendError(f"{type(exc).__name__}: {exc}") from exc
        error = _classify(response)
        if error is not None:
            raise error
        try:
            data = response.json()
        except ValueError as exc:
            raise EmailSendError("provider returned a non-JSON response") from exc
        return data if isinstance(data, dict) else {}


class PostmarkProvider(_HttpProvider):
    URL = "https://api.postmarkapp.com/email"

    def send(self, message: EmailMessage) -> str:
        body: dict[str, object] = {
            "From": self._from,
            "To": message.to,
            "Subject": message.subject,
            "TextBody": message.text,
            "MessageStream": "outbound",
            "Headers": [{"Name": k, "Value": v} for k, v in message.headers.items()],
        }
        if message.html:
            body["HtmlBody"] = message.html
        data = self._post(self.URL, {"X-Postmark-Server-Token": self._api_key}, body)
        # Postmark reports some rejections with HTTP 200 and a non-zero ErrorCode.
        if data.get("ErrorCode", 0) not in (0, "0"):
            raise EmailSendError(str(data.get("Message", "rejected")), retryable=False)
        return str(data.get("MessageID", ""))


class ResendProvider(_HttpProvider):
    URL = "https://api.resend.com/emails"

    def send(self, message: EmailMessage) -> str:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if message.idempotency_key:
            headers["Idempotency-Key"] = message.idempotency_key
        body: dict[str, object] = {
            "from": self._from,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
            "headers": message.headers,
        }
        if message.html:
            body["html"] = message.html
        return str(self._post(self.URL, headers, body).get("id", ""))


def build_provider(settings: Settings) -> EmailProvider:
    name = settings.email_provider.strip().lower()
    if name in ("", "console"):
        return ConsoleProvider()
    if name == "fake":
        return FakeProvider()
    if name == "postmark":
        return PostmarkProvider(settings.email_api_key, settings.email_from)
    if name == "resend":
        return ResendProvider(settings.email_api_key, settings.email_from)
    raise RuntimeError(f"unknown EMAIL_PROVIDER {settings.email_provider!r}")


_provider: EmailProvider | None = None


def get_provider() -> EmailProvider:
    """The configured provider, built once per process."""
    global _provider
    if _provider is None:
        _provider = build_provider(get_settings())
    return _provider


def set_provider(provider: EmailProvider | None) -> None:
    """Replace (or with ``None`` reset) the process-wide provider. For tests."""
    global _provider
    _provider = provider
