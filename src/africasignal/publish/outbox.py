"""The email outbox: queue messages transactionally, send them from ``dispatch_outbox``.

Delivery is at-least-once. A row is sent, then marked ``sent`` and committed, so a crash between
the two sends that one message again; the provider gets the row's ``dedupe_key`` as its
idempotency key to absorb that. Status meanings:

- ``pending``: never tried
- ``failed``: tried and failed, will be retried at ``next_attempt_at``
- ``sent``: the provider accepted it
- ``dead``: gave up (too many attempts, a permanent error, or cancelled because it was superseded)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal.jobs.queue import backoff_seconds
from africasignal.models import AppUser, Outbox
from africasignal.publish import email_render
from africasignal.publish.email import (
    EmailMessage,
    EmailNotConfigured,
    EmailProvider,
    EmailSendError,
    get_provider,
)
from africasignal.publish.suspension import publication_suspended

log = logging.getLogger("africasignal.outbox")

BATCH_SIZE = 50
MAX_ATTEMPTS = 5

# Payload keys that grant access (a live sign-in link). They are removed once a row is sent or
# dead, so a leaked database does not hold working links.
SECRET_PAYLOAD_KEYS = ("link",)

# Kinds that are notifications, held while publication is suspended (plan B12 kill switch).
# Sign-in emails are not notifications and are still sent.
HELD_WHILE_SUSPENDED = ("email_digest", "email_correction")


def enqueue_email(
    session: Session, kind: str, payload: dict[str, Any], dedupe_key: str
) -> int | None:
    """Queue one email. Returns the row id, or ``None`` when ``dedupe_key`` already exists.
    The caller commits."""
    return session.execute(
        pg_insert(Outbox)
        .values(kind=kind, payload=payload, dedupe_key=dedupe_key)
        .on_conflict_do_nothing(index_elements=[Outbox.dedupe_key])
        .returning(Outbox.id)
    ).scalar_one_or_none()


@dataclass
class DispatchResult:
    sent: int = 0
    retried: int = 0
    dead: int = 0
    held: int = 0


def _scrub(row: Outbox) -> None:
    row.payload = {k: v for k, v in row.payload.items() if k not in SECRET_PAYLOAD_KEYS}


def _build_message(session: Session, row: Outbox) -> EmailMessage | None:
    """Render a row. ``None`` when the recipient is gone or has not verified their address."""
    user = session.get(AppUser, row.payload["user_id"])
    if user is None or user.deleted_at is not None:
        return None
    if row.kind == "email_login":  # sent to an address that is not verified yet
        return email_render.render_login(user.email, row.payload, row.dedupe_key)
    if user.email_verified_at is None:
        return None
    base = email_render.resolve_base_url(session)  # read now, so a console change applies
    if row.kind == "email_correction":
        return email_render.render_correction(
            user.email, user.id, row.payload, row.dedupe_key, base=base
        )
    if row.kind == "email_digest":
        if not user.digest_opt_in:
            return None
        return email_render.render_digest(
            user.email, user.id, row.payload, row.dedupe_key, base=base
        )
    raise ValueError(f"unknown outbox kind {row.kind!r}")


def dispatch_pending(
    session: Session, provider: EmailProvider, now: datetime, limit: int = BATCH_SIZE
) -> DispatchResult:
    """Send up to ``limit`` due rows. Commits after every row so a crash loses no progress."""
    result = DispatchResult()
    suspended = publication_suspended(session)
    due = select(Outbox).where(
        Outbox.status.in_(("pending", "failed")), Outbox.next_attempt_at <= now
    )
    if suspended:
        result.held = (
            session.scalar(
                select(func.count()).select_from(
                    due.where(Outbox.kind.in_(HELD_WHILE_SUSPENDED)).subquery()
                )
            )
            or 0
        )
        due = due.where(Outbox.kind.notin_(HELD_WHILE_SUSPENDED))
    rows = session.scalars(
        due.order_by(Outbox.next_attempt_at, Outbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for row in rows:
        _send_one(session, provider, row, now, result)
        session.commit()
    return result


def _send_one(
    session: Session, provider: EmailProvider, row: Outbox, now: datetime, result: DispatchResult
) -> None:
    row.attempts += 1
    try:
        message = _build_message(session, row)
        if message is None:
            row.status, row.last_error = "dead", "recipient unavailable"
            _scrub(row)
            result.dead += 1
            return
        row.provider_message_id = provider.send(message)
    except EmailSendError as exc:
        _record_failure(row, str(exc), retryable=exc.retryable, now=now, result=result)
        return
    except Exception as exc:  # a rendering bug or a provider bug must not stall the batch
        log.exception("outbox row %s failed", row.id)
        _record_failure(row, f"{type(exc).__name__}: {exc}", retryable=True, now=now, result=result)
        return
    row.status, row.last_error = "sent", None
    _scrub(row)
    result.sent += 1


def _record_failure(
    row: Outbox, error: str, *, retryable: bool, now: datetime, result: DispatchResult
) -> None:
    row.last_error = error[:500]
    if retryable and row.attempts < MAX_ATTEMPTS:
        row.status = "failed"
        row.next_attempt_at = now + timedelta(seconds=backoff_seconds(row.attempts))
        result.retried += 1
    else:
        row.status = "dead"
        _scrub(row)
        result.dead += 1


def dispatch_with_configured_provider(session: Session, now: datetime) -> DispatchResult | None:
    """Send due rows with the provider the operator configured in the console. ``None`` when no
    usable provider is configured: nothing is sent, nothing is marked, and the rows wait."""
    try:
        provider = get_provider(session)
    except EmailNotConfigured as exc:
        log.warning("email not sent: %s", exc)
        return None
    return dispatch_pending(session, provider, now)
