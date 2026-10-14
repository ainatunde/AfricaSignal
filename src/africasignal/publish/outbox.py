"""The email outbox: queue messages transactionally, send them from ``dispatch_outbox``.

The exact authorization and content hash are committed before a provider call. Ambiguous outcomes
are held for operator reconciliation rather than retried. Status meanings:

- ``pending``: never tried
- ``failed``: tried and failed, will be retried at ``next_attempt_at``
- ``sent``: the provider accepted it (mailbox delivery is not confirmed)
- ``simulated``: a console or test adapter recorded no external send
- ``outcome_unknown``: the request may have reached the provider; do not retry blindly
- ``dead``: gave up (too many attempts, a permanent error, or cancelled because it was superseded)
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal import settings_store
from africasignal.assess.retrieval import assessment_passages
from africasignal.config import get_settings
from africasignal.jobs.execution import fenced_effect
from africasignal.jobs.queue import backoff_seconds
from africasignal.models import (
    AppUser,
    AssessmentVersion,
    DeliveryAttempt,
    EditorialInsightDraft,
    Follow,
    LoginToken,
    Outbox,
    Situation,
)
from africasignal.publish import email_render
from africasignal.publish.eligibility import publication_eligibility
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
HELD_WHILE_SUSPENDED = ("email_digest", "email_correction", "email_insight")


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
    simulated: int = 0
    unknown: int = 0
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
    if row.kind == "email_insight":
        if user.email_verified_at is None or not user.insight_email_opt_in:
            return None
        version_id = row.payload.get("assessment_version_id")
        draft = session.get(EditorialInsightDraft, row.payload.get("editorial_draft_id"))
        send_time = now or datetime.now(UTC)
        if (
            draft is None
            or draft.status != "email_queued"
            or draft.assessment_version_id != version_id
            or not _current_version(session, version_id, send_time)
        ):
            return None
        available_claim_ids = {
            item.claim_id for item in assessment_passages(session, version_id, now=send_time)
        }
        if not set(draft.evidence_claim_ids).issubset(available_claim_ids):
            return None
        situation_id = session.scalar(
            select(Situation.id)
            .join(AssessmentVersion, AssessmentVersion.situation_id == Situation.id)
            .where(AssessmentVersion.id == version_id)
        )
        if (
            situation_id is None
            or session.scalar(
                select(Follow.id).where(
                    Follow.user_id == user.id, Follow.situation_id == situation_id
                )
            )
            is None
        ):
            return None
        base = email_render.resolve_base_url(session)
        return email_render.render_insight(
            user.email,
            user.id,
            {"content": draft.content, "item": row.payload["item"]},
            row.dedupe_key,
            base=base,
        )
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
    version = session.get(AssessmentVersion, version_id)
    return (
        version is not None
        and publication_eligibility(
            session,
            version,
            channel="email",
            now=now,
            allow_withdrawal_notice=withdrawal,
        ).eligible
    )


def dispatch_pending(
    session: Session, provider: EmailProvider, now: datetime, limit: int = BATCH_SIZE
) -> DispatchResult:
    """Send due rows after persisting an attempt fence and authorization snapshot."""
    result = DispatchResult()
    stale = list(
        session.scalars(
            select(Outbox)
            .where(
                Outbox.status == "sending",
                Outbox.attempt_started_at < now - timedelta(minutes=5),
            )
            .order_by(Outbox.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for stale_row in stale:
        stale_row.status = "outcome_unknown"
        stale_row.last_error = (
            "The sender stopped during the request; confirm the mailbox before retrying."
        )
        attempt = session.scalar(
            select(DeliveryAttempt)
            .where(
                DeliveryAttempt.outbox_id == stale_row.id,
                DeliveryAttempt.status == "prepared",
            )
            .order_by(DeliveryAttempt.attempt_no.desc())
            .limit(1)
            .with_for_update()
        )
        if attempt is not None:
            attempt.status = "outcome_unknown"
            attempt.completed_at = now
            attempt.outcome_detail = stale_row.last_error
        _scrub(stale_row)
        result.unknown += 1
    if stale:
        session.commit()

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
    except EmailSendError as exc:
        _record_failure(row, str(exc), retryable=exc.retryable, now=now, result=result)
        return
    except Exception as exc:
        log.exception("outbox row %s could not be prepared", row.id)
        _record_failure(
            row,
            f"{type(exc).__name__}: {exc}",
            retryable=False,
            now=now,
            result=result,
        )
        return

    row_id = row.id
    content_hash = _message_hash(message)
    user_id = row.payload.get("user_id")
    destination_hash = hashlib.sha256(message.to.encode("utf-8")).hexdigest()
    version_ids = _assessment_version_ids(row)
    eligibility = []
    for version_id in version_ids:
        version = session.get(AssessmentVersion, version_id)
        if version is not None:
            allow_withdrawal = (
                row.kind == "email_correction"
                and row.payload.get("notification_kind") == "withdrawal"
            )
            eligibility.append(
                {
                    "assessment_version_id": version_id,
                    "decision": publication_eligibility(
                        session,
                        version,
                        channel="email",
                        now=now,
                        allow_withdrawal_notice=allow_withdrawal,
                    ).snapshot(),
                }
            )
    user = session.get(AppUser, user_id) if isinstance(user_id, int) else None
    provider_name = type(provider).__name__
    sender = settings_store.get(session, "email_from") or get_settings().email_from
    draft = (
        session.get(EditorialInsightDraft, row.payload.get("editorial_draft_id"))
        if row.kind == "email_insight"
        else None
    )
    authorization = {
        "kind": row.kind,
        "outbox_id": row.id,
        "dedupe_key": row.dedupe_key,
        "recipient_user_id": user.id if user is not None else None,
        "email_verified": user.email_verified_at is not None if user is not None else False,
        "digest_opt_in": user.digest_opt_in if user is not None else False,
        "insight_email_opt_in": user.insight_email_opt_in if user is not None else False,
        "provider_adapter": provider_name,
        "sender_account": sender,
        "assessment_dependencies": eligibility,
        "editorial_approval": (
            {
                "draft_id": draft.id,
                "status": draft.status,
                "reviewed_by_operator_id": draft.reviewed_by_operator_id,
                "reviewed_at": draft.reviewed_at.isoformat() if draft.reviewed_at else None,
                "prompt_version": draft.prompt_version,
            }
            if draft is not None
            else None
        ),
    }
    row.status = "sending"
    row.attempt_started_at = now
    row.content_sha256 = content_hash
    row.destination_sha256 = destination_hash
    row.authorization_snapshot = authorization
    attempt = DeliveryAttempt(
        outbox_id=row.id,
        attempt_no=row.attempts,
        channel="email",
        content_sha256=content_hash,
        destination_sha256=destination_hash,
        destination_account_id=sender or provider_name,
        target_reference=f"app_user:{user_id}" if isinstance(user_id, int) else None,
        assessment_version_ids=version_ids,
        authorization_snapshot=authorization,
        status="prepared",
        dedupe_key=f"email-outbox:{row.id}:attempt:{row.attempts}",
        attempted_at=now,
    )
    session.add(attempt)
    session.commit()  # persist the no-retry fence before the provider side effect
    attempt_id = attempt.id

    # Re-render against current consent, evidence and recipient immediately before dispatch.
    current_row = session.scalar(
        select(Outbox).where(Outbox.id == row_id).execution_options(populate_existing=True)
    )
    try:
        current_message = (
            _build_message(session, current_row, now) if current_row is not None else None
        )
    except Exception as exc:
        current_message = None
        preflight_error: str | None = f"{type(exc).__name__}: {exc}"
    else:
        preflight_error = None
    if current_row is None or current_row.status != "sending" or current_message is None:
        _finish_attempt(
            session,
            row_id,
            attempt_id,
            "dead",
            now,
            attempt_status="cancelled",
            error=(
                preflight_error
                or "Current consent, evidence, or recipient no longer authorizes this send."
            ),
        )
        result.dead += 1
        return
    if _message_hash(current_message) != content_hash:
        _finish_attempt(
            session,
            row_id,
            attempt_id,
            "dead",
            now,
            attempt_status="cancelled",
            error="Rendered content or recipient changed after authorization; review again.",
        )
        result.dead += 1
        return
    session.commit()

    try:
        with fenced_effect():
            receipt = provider.send(current_message)
        if not receipt or not receipt.strip():
            raise EmailSendError(
                "provider returned no message id; outcome uncertain",
                retryable=False,
                outcome_unknown=True,
            )
    except EmailSendError as exc:
        _finish_email_error(session, row_id, attempt_id, exc, now, result)
        return
    except Exception as exc:
        log.exception("outbox row %s failed without a definitive provider outcome", row_id)
        _finish_email_error(
            session,
            row_id,
            attempt_id,
            EmailSendError(f"{type(exc).__name__}: {exc}", retryable=False, outcome_unknown=True),
            now,
            result,
        )
        return

    simulated = bool(getattr(provider, "simulated", False))
    finished = _finish_attempt(
        session,
        row_id,
        attempt_id,
        "simulated" if simulated else "sent",
        now,
        receipt=receipt,
    )
    if not finished:
        result.unknown += 1
        return
    if simulated:
        result.simulated += 1
    else:
        result.sent += 1


def _message_hash(message: EmailMessage) -> str:
    exact_message = {
        "to": message.to,
        "subject": message.subject,
        "text": message.text,
        "html": message.html,
        "headers": dict(sorted(message.headers.items())),
        "idempotency_key": message.idempotency_key,
    }
    encoded = json.dumps(exact_message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _assessment_version_ids(row: Outbox) -> list[int]:
    found: set[int] = set()
    direct = row.payload.get("assessment_version_id")
    if isinstance(direct, int) and not isinstance(direct, bool):
        found.add(direct)
    for section in ("followed", "top"):
        for item in row.payload.get(section, []):
            value = item.get("assessment_version_id") if isinstance(item, dict) else None
            if isinstance(value, int) and not isinstance(value, bool):
                found.add(value)
    return sorted(found)


def _finish_attempt(
    session: Session,
    row_id: int,
    attempt_id: int,
    status: str,
    now: datetime,
    *,
    attempt_status: str | None = None,
    receipt: str | None = None,
    error: str | None = None,
    next_attempt_at: datetime | None = None,
) -> bool:
    row = session.scalar(select(Outbox).where(Outbox.id == row_id).with_for_update())
    attempt = session.scalar(
        select(DeliveryAttempt).where(DeliveryAttempt.id == attempt_id).with_for_update()
    )
    if row is None or attempt is None:
        session.rollback()
        return False
    active_attempt = row.status == "sending" and attempt.status == "prepared"
    late_resolution = (
        row.status == "outcome_unknown"
        and attempt.status == "outcome_unknown"
        and status in {"sent", "simulated", "failed", "dead", "outcome_unknown"}
    )
    if not active_attempt and not late_resolution:
        session.rollback()
        return False
    row.status = status
    row.provider_message_id = receipt
    row.last_error = error[:500] if error else None
    if next_attempt_at is not None:
        row.next_attempt_at = next_attempt_at
    attempt.status = attempt_status or ("rejected" if status in ("failed", "dead") else status)
    attempt.provider_receipt_id = receipt
    attempt.completed_at = now
    attempt.outcome_detail = error[:500] if error else None
    if status in ("sent", "simulated", "dead", "outcome_unknown", "cancelled"):
        _scrub(row)
    session.commit()
    return True


def _finish_email_error(
    session: Session,
    row_id: int,
    attempt_id: int,
    error: EmailSendError,
    now: datetime,
    result: DispatchResult,
) -> None:
    row = session.get(Outbox, row_id)
    if row is None:
        session.rollback()
        return
    message = str(error)[:500]
    if error.outcome_unknown:
        _finish_attempt(session, row_id, attempt_id, "outcome_unknown", now, error=message)
        result.unknown += 1
    elif error.retryable and row.attempts < MAX_ATTEMPTS:
        finished = _finish_attempt(
            session,
            row_id,
            attempt_id,
            "failed",
            now,
            error=message,
            next_attempt_at=now + timedelta(seconds=backoff_seconds(row.attempts)),
        )
        if finished:
            result.retried += 1
        else:
            result.unknown += 1
    else:
        finished = _finish_attempt(session, row_id, attempt_id, "dead", now, error=message)
        if finished:
            result.dead += 1
        else:
            result.unknown += 1


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
