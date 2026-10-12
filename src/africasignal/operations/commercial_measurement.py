"""Short-lived click tokens, bounded event ingestion, and delivery observations."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal.models import DeliveryEvent
from africasignal.operations.commercial_bookings import (
    PlacementDecision,
    eligible_placement,
)
from africasignal.operations.commercial_campaigns import CreativeDraft
from africasignal.publish.tokens import sign, verify

TOKEN_PURPOSE = "commercial-click-v1"
TOKEN_TTL_SECONDS = 15 * 60
MAX_TOKEN_BYTES = 1024
RAW_EVENT_RETENTION = timedelta(days=30)
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")


class ClickTokenError(ValueError):
    """Safe token/redirect failure with a bounded reason code."""

    def __init__(self, reason_code: str = "token_invalid") -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class ClickClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    event_kind: Literal["click"]
    booking_id: int = Field(ge=1, strict=True)
    creative_version_id: int = Field(ge=1, strict=True)
    booking_revision: int = Field(ge=1, strict=True)
    issued_at: int = Field(ge=0, strict=True)
    expires_at: int = Field(ge=1, strict=True)
    nonce: str = Field(min_length=22, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class ClickTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    booking_id: int
    creative_version_id: int
    booking_revision: int
    deduplication_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_url: str


class DeliveryEnvelope(BaseModel):
    """The complete, deliberately narrow public input for first-party click collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_schema_version: Literal[1]
    event_kind: Literal["click"]
    token: str = Field(min_length=1, max_length=MAX_TOKEN_BYTES)


class EventReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["accepted", "duplicate"]
    reason_code: Literal["accepted", "event_duplicate"]
    metric: Literal["click"]
    booking_id: int
    creative_version_id: int
    destination_url: str


def _epoch(moment: datetime) -> int:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("click token time must be timezone-aware")
    return int(moment.astimezone(UTC).timestamp())


def issue_click_token(
    *,
    booking_id: int,
    creative_version_id: int,
    booking_revision: int,
    at: datetime | None = None,
) -> str:
    now = datetime.now(UTC) if at is None else at
    issued = _epoch(now)
    claims = ClickClaims(
        schema_version=1,
        event_kind="click",
        booking_id=booking_id,
        creative_version_id=creative_version_id,
        booking_revision=booking_revision,
        issued_at=issued,
        expires_at=issued + TOKEN_TTL_SECONDS,
        nonce=secrets.token_urlsafe(18),
    )
    payload = json.dumps(claims.model_dump(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    value = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return sign(TOKEN_PURPOSE, value)


def verify_click_token(token: str, *, at: datetime | None = None) -> ClickClaims:
    if not token or len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise ClickTokenError()
    try:
        value = verify(TOKEN_PURPOSE, token)
    except (RuntimeError, ValueError):
        raise ClickTokenError() from None
    if value is None or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ClickTokenError()
    try:
        payload = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        raw = json.loads(payload)
        claims = ClickClaims.model_validate(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ClickTokenError() from None
    moment = datetime.now(UTC) if at is None else at
    now_epoch = _epoch(moment)
    if (
        claims.expires_at <= now_epoch
        or claims.expires_at <= claims.issued_at
        or claims.expires_at - claims.issued_at > TOKEN_TTL_SECONDS
        or claims.issued_at > now_epoch + 60
        or not NONCE_RE.fullmatch(claims.nonce)
    ):
        raise ClickTokenError()
    return claims


def validate_click_destination(
    session: Session, token: str, *, at: datetime | None = None
) -> ClickTarget:
    moment = datetime.now(UTC) if at is None else at
    claims = verify_click_token(token, at=moment)
    try:
        decision = eligible_placement(
            session,
            surface="explore_topic",
            topic=_topic_for_booking(session, claims.booking_id),
            item_code=None,
            at=moment,
        )
    except Exception:
        # Failed lookups, suspension checks, and control reads must never create a redirect.
        raise ClickTokenError("invalid_context") from None
    if not decision.eligible:
        if decision.reason_code in ("disabled", "suspended", "invalid_context"):
            raise ClickTokenError(decision.reason_code)
        raise ClickTokenError()
    if (
        decision.booking_id != claims.booking_id
        or decision.booking_revision != claims.booking_revision
        or decision.creative_version_id != claims.creative_version_id
        or decision.destination_url is None
        or decision.body_text is None
    ):
        raise ClickTokenError()
    try:
        creative = CreativeDraft(
            body_text=decision.body_text,
            destination_url=decision.destination_url,
            asset_key=decision.asset_key,
            alt_text=decision.alt_text,
        )
    except ValueError:
        raise ClickTokenError() from None
    return ClickTarget(
        booking_id=claims.booking_id,
        creative_version_id=claims.creative_version_id,
        booking_revision=claims.booking_revision,
        deduplication_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        destination_url=creative.destination_url,
    )


def record_delivery_event(
    session: Session,
    *,
    envelope: DeliveryEnvelope,
    received_at: datetime,
) -> EventReceipt:
    """Validate, idempotently store one click and return the current safe redirect target.

    The caller owns commit/rollback. The raw signed token is used only to derive a one-way
    deduplication hash and is never sent to SQL or persisted.
    """
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("event receive time must be timezone-aware")
    moment = received_at.astimezone(UTC)
    target = validate_click_destination(session, envelope.token, at=moment)
    statement = (
        pg_insert(DeliveryEvent)
        .values(
            booking_id=target.booking_id,
            creative_version_id=target.creative_version_id,
            booking_revision=target.booking_revision,
            event_schema_version=envelope.event_schema_version,
            metric="click",
            metric_version=1,
            deduplication_hash=target.deduplication_hash,
            received_at=moment,
            validity_status="accepted",
            rejection_reason=None,
            retention_until=moment + RAW_EVENT_RETENTION,
        )
        .on_conflict_do_nothing(index_elements=[DeliveryEvent.deduplication_hash])
        .returning(DeliveryEvent.id)
    )
    event_id = session.scalar(statement)
    return EventReceipt(
        status="accepted" if event_id is not None else "duplicate",
        reason_code="accepted" if event_id is not None else "event_duplicate",
        metric="click",
        booking_id=target.booking_id,
        creative_version_id=target.creative_version_id,
        destination_url=target.destination_url,
    )


def record_placement_observation(
    session: Session,
    *,
    decision: PlacementDecision,
    metric: Literal["eligible_opportunity", "server_render"],
    received_at: datetime,
) -> None:
    """Store an anonymous server observation for one rendered eligible placement."""
    if not decision.eligible or any(
        value is None
        for value in (
            decision.booking_id,
            decision.booking_revision,
            decision.creative_version_id,
            decision.topic,
        )
    ):
        raise ValueError("only an eligible placement can be observed")
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("event receive time must be timezone-aware")
    moment = received_at.astimezone(UTC)
    digest = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    statement = pg_insert(DeliveryEvent).values(
        booking_id=decision.booking_id,
        creative_version_id=decision.creative_version_id,
        booking_revision=decision.booking_revision,
        event_schema_version=1,
        metric=metric,
        metric_version=1,
        deduplication_hash=digest,
        received_at=moment,
        validity_status="accepted",
        rejection_reason=None,
        retention_until=moment + RAW_EVENT_RETENTION,
    )
    session.execute(statement)


def _topic_for_booking(session: Session, booking_id: int) -> Literal["energy", "food"]:
    from africasignal.models import PlacementBooking

    row = session.get(PlacementBooking, booking_id)
    if row is None or row.topic not in ("energy", "food"):
        raise ClickTokenError()
    return row.topic  # type: ignore[return-value]
