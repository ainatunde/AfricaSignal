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

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, or_, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal.config import get_settings
from africasignal.jobs.execution import fenced_effect
from africasignal.jobs.queue import backoff_seconds
from africasignal.models import AppUser, AssessmentVersion, LoginToken, Outbox, Situation
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


def _build_message(
    session: Session, row: Outbox, now: datetime | None = None
) -> EmailMessage | None:
    """Render a row. ``None`` when the recipient is gone or has not verified their address."""
    user = session.scalar(
        select(AppUser)
        .where(AppUser.id == row.payload["user_id"])
        .execution_options(populate_existing=True)
    )
    if user is None or user.deleted_at is not None:
        return None
    if row.kind == "email_login":  # sent to an address that is not verified yet
        raw = parse_qs(urlparse(row.payload.get("link", "")).query).get("token", [""])[0]
        valid = (
            session.scalar(
                select(LoginToken.id).where(
                    LoginToken.user_id == user.id,
                    LoginToken.token_sha256 == hashlib.sha256(raw.encode()).hexdigest(),
                    LoginToken.used_at.is_(None),
                    LoginToken.expires_at > (now or datetime.now(UTC)),
                )
            )
            if raw
            else None
        )
        if valid is None:
            return None
        return email_render.render_login(user.email, row.payload, row.dedupe_key)
    if user.email_verified_at is None or not user.digest_opt_in:
        return None
    base = email_render.resolve_base_url(session)  # read now, so a console change applies
    if row.kind == "email_correction":
        version_id = row.payload.get("assessment_version_id")
        if version_id is not None and not _current_version(
            session,
            version_id,
            now or datetime.now(UTC),
            withdrawal=row.payload.get("notification_kind") == "withdrawal",
        ):
            return None
        return email_render.render_correction(
            user.email, user.id, row.payload, row.dedupe_key, base=base
        )
    if row.kind == "email_digest":
        if not user.digest_opt_in:
            return None
        payload = dict(row.payload)
        had_items = bool(payload.get("followed") or payload.get("top"))
        for section in ("followed", "top"):
            payload[section] = [
                item
                for item in payload.get(section, [])
                if _current_version(
                    session, item.get("assessment_version_id"), now or datetime.now(UTC)
                )
            ]
        if had_items and not (payload["followed"] or payload["top"]):
            return None
        return email_render.render_digest(user.email, user.id, payload, row.dedupe_key, base=base)
    raise ValueError(f"unknown outbox kind {row.kind!r}")


def _current_version(
    session: Session, version_id: int | None, now: datetime, *, withdrawal: bool = False
) -> bool:
    if version_id is None:
        return False
    return (
        session.scalar(
            select(AssessmentVersion.id)
            .join(Situation, Situation.current_version_id == AssessmentVersion.id)
            .where(
                AssessmentVersion.id == version_id,
                AssessmentVersion.status == ("withdrawn" if withdrawal else "published"),
                or_(AssessmentVersion.valid_until.is_(None), AssessmentVersion.valid_until > now)
                if not withdrawal
                else true(),
            )
        )
        is not None
    )


def dispatch_pending(
    session: Session, provider: EmailProvider, now: datetime, limit: int = BATCH_SIZE
) -> DispatchResult:
    """Send up to ``limit`` due rows. Commits after every row so a crash loses no progress."""
    result = DispatchResult()
    # Lock only the row whose send this transaction owns. A prefetched locked batch loses
    # every remaining lock when the first row commits, allowing another worker to send it.
    for _ in range(limit):
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
        row = session.scalar(
            due.order_by(Outbox.next_attempt_at, Outbox.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if row is None:
            break
        # Suspension applies between sends; an already-started provider request is in flight.
        _send_one(session, provider, row, now, result)
        session.commit()
    return result


def _send_one(
    session: Session, provider: EmailProvider, row: Outbox, now: datetime, result: DispatchResult
) -> None:
    row.attempts += 1
    try:
        message = _build_message(session, row, now)
        if message is None:
            row.status, row.last_error = "dead", "recipient unavailable"
            _scrub(row)
            result.dead += 1
            return
        settings = get_settings()
        if (
            settings.env == "staging"
            and message.to.strip().casefold() not in settings.staging_email_recipients
        ):
            raise EmailSendError("staging recipient is not allowlisted", retryable=False)
        with fenced_effect():
            row.provider_message_id = provider.send(message)
        if not row.provider_message_id or not row.provider_message_id.strip():
            raise EmailSendError("provider returned no message id; outcome uncertain")
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
