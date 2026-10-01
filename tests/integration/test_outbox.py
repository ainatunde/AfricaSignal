"""The outbox dispatcher (plan B4): at-least-once sending with retries, on PostgreSQL."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import LoginToken, Outbox, Setting
from africasignal.publish.email import EmailSendError, FakeProvider
from africasignal.publish.login_tokens import hash_token
from africasignal.publish.outbox import MAX_ATTEMPTS, dispatch_pending, enqueue_email
from tests.integration.email_support import NOW, add_user

LINK = "http://localhost:8000/signin/verify?token=secret-token"


def login_row(session: Session, user_id: int, key: str = "login:1") -> Outbox:
    if session.scalar(select(LoginToken.id).where(LoginToken.user_id == user_id)) is None:
        session.add(
            LoginToken(
                user_id=user_id,
                token_sha256=hash_token("secret-token"),
                expires_at=NOW + timedelta(hours=1),
            )
        )
        session.flush()
    enqueue_email(session, "email_login", {"user_id": user_id, "link": LINK}, key)
    return session.scalars(select(Outbox).where(Outbox.dedupe_key == key)).one()


def test_enqueue_is_idempotent_on_the_dedupe_key(session: Session) -> None:
    user = add_user(session, "a@example.com")
    assert (
        enqueue_email(session, "email_login", {"user_id": user.id, "link": LINK}, "k") is not None
    )
    assert enqueue_email(session, "email_login", {"user_id": user.id, "link": LINK}, "k") is None


def test_sends_marks_sent_and_scrubs_the_link(session: Session) -> None:
    user = add_user(session, "a@example.com", verified=False)  # a first sign-in is unverified
    row = login_row(session, user.id)
    provider = FakeProvider()

    result = dispatch_pending(session, provider, NOW)

    assert result.sent == 1
    assert [m.to for m in provider.sent] == ["a@example.com"]
    assert LINK in provider.sent[0].text
    assert provider.sent[0].idempotency_key == "login:1"
    assert (row.status, row.provider_message_id, row.attempts) == ("sent", "fake-1", 1)
    assert "link" not in row.payload  # a leaked database holds no working sign-in link

    assert dispatch_pending(session, provider, NOW).sent == 0  # nothing is sent twice
    assert len(provider.sent) == 1


def test_retryable_failure_backs_off_then_goes_dead(session: Session) -> None:
    user = add_user(session, "a@example.com")
    row = login_row(session, user.id)
    provider = FakeProvider(fail_with=EmailSendError("HTTP 503"))

    now = NOW
    for attempt in range(1, MAX_ATTEMPTS):
        assert dispatch_pending(session, provider, now).retried == 1
        assert (row.status, row.attempts, row.last_error) == ("failed", attempt, "HTTP 503")
        assert row.next_attempt_at > now
        assert dispatch_pending(session, provider, now).retried == 0  # not due yet
        now = row.next_attempt_at + timedelta(seconds=1)

    assert dispatch_pending(session, provider, now).dead == 1
    assert (row.status, row.attempts) == ("dead", MAX_ATTEMPTS)
    assert "link" not in row.payload


def test_a_retry_that_succeeds_is_sent(session: Session) -> None:
    user = add_user(session, "a@example.com")
    row = login_row(session, user.id)
    provider = FakeProvider(fail_with=EmailSendError("timeout"))
    dispatch_pending(session, provider, NOW)
    provider.fail_with = None
    result = dispatch_pending(session, provider, row.next_attempt_at + timedelta(seconds=1))
    assert result.sent == 1 and row.status == "sent" and row.attempts == 2


def test_permanent_failure_goes_dead_at_once(session: Session) -> None:
    user = add_user(session, "a@example.com")
    row = login_row(session, user.id)
    provider = FakeProvider(fail_with=EmailSendError("bad address", retryable=False))
    assert dispatch_pending(session, provider, NOW).dead == 1
    assert (row.status, row.attempts) == ("dead", 1)


def test_one_bad_row_does_not_stall_the_batch(session: Session) -> None:
    good = add_user(session, "good@example.com")
    login_row(session, good.id, "login:good")
    enqueue_email(session, "email_login", {"user_id": 999999, "link": LINK}, "login:gone")

    provider = FakeProvider()
    result = dispatch_pending(session, provider, NOW)
    assert (result.sent, result.dead) == (1, 1)
    gone = session.scalars(select(Outbox).where(Outbox.dedupe_key == "login:gone")).one()
    assert (gone.status, gone.last_error) == ("dead", "recipient unavailable")


def test_batch_limit(session: Session) -> None:
    user = add_user(session, "a@example.com")
    for n in range(3):
        login_row(session, user.id, f"login:{n}")
    provider = FakeProvider()
    assert dispatch_pending(session, provider, NOW, limit=2).sent == 2
    assert dispatch_pending(session, provider, NOW, limit=2).sent == 1


def test_digest_is_held_while_publication_is_suspended_but_login_is_sent(
    session: Session,
) -> None:
    user = add_user(session, "a@example.com", digest=True)
    login_row(session, user.id)
    payload = {"user_id": user.id, "week": "2026-W40", "followed": [], "top": []}
    enqueue_email(session, "email_digest", payload, "digest:1")
    session.add(Setting(key="publication_suspended", value=True))
    session.flush()
    provider = FakeProvider()

    result = dispatch_pending(session, provider, NOW)
    assert (result.sent, result.held) == (1, 1)
    assert [m.subject for m in provider.sent] == ["Your AfricaSignal sign-in link"]

    session.get(Setting, "publication_suspended").value = False  # type: ignore[union-attr]
    session.flush()
    assert dispatch_pending(session, provider, NOW).sent == 1


@pytest.mark.parametrize("kind", ["email_digest", "email_correction"])
def test_unverified_or_opted_out_recipients_get_no_notification_email(
    session: Session, kind: str
) -> None:
    unverified = add_user(session, "u@example.com", verified=False, digest=True)
    opted_out = add_user(session, "o@example.com", digest=False)
    item = {"slug": "s", "title": "T", "headline": "H", "scope_label": "L", "change_summary": None}
    for user in (unverified, opted_out):
        payload = {
            "user_id": user.id,
            "week": "2026-W40",
            "followed": [],
            "top": [],
            "notification_kind": "correction",
            "item": item,
        }
        enqueue_email(session, kind, payload, f"{kind}:{user.id}")
    provider = FakeProvider()
    result = dispatch_pending(session, provider, NOW)
    assert (result.sent, result.dead) == (0, 2)
    assert provider.sent == []


@pytest.mark.parametrize("ended", ["expired", "used", "missing"])
def test_login_delivery_rechecks_token(session: Session, ended: str) -> None:
    user = add_user(session, "expired@example.com")
    row = login_row(session, user.id)
    token = session.scalars(select(LoginToken)).one()
    if ended == "expired":
        token.expires_at = NOW
    elif ended == "used":
        token.used_at = NOW
    else:
        session.delete(token)
    session.flush()
    provider = FakeProvider()
    assert dispatch_pending(session, provider, NOW).dead == 1
    assert provider.sent == []
    assert "link" not in row.payload
