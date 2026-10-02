"""Exclusive topic-scoped booking approval, activation and public eligibility."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.models import (
    Campaign,
    ContentContext,
    CreativeVersion,
    Operator,
    PlacementBooking,
    Situation,
    Sponsor,
)
from africasignal.operations.commercial_context import ContextError, classify_content
from africasignal.operations.commercial_context_contract import ContextRef
from africasignal.operations.commercial_controls import current as commercial_state
from africasignal.publish.suspension import publication_suspended

Topic = Literal["energy", "food"]
Surface = Literal["explore_topic"]
BookingStatus = Literal["draft", "approved", "active", "paused", "ended"]


class BookingError(ValueError):
    """Safe error for booking operations."""


class BookingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    surface: Surface = "explore_topic"
    topic: Topic
    starts_at: datetime
    ends_at: datetime
    creative_version_id: int = Field(ge=1)
    exclusive: Literal[True] = True

    @field_validator("starts_at", "ends_at")
    @classmethod
    def utc_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("booking times must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def positive_interval(self) -> BookingDraft:
        if self.starts_at >= self.ends_at:
            raise ValueError("booking end must be after its start")
        return self


class BookingView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    campaign_id: int
    surface: Surface
    topic: Topic
    starts_at: datetime
    ends_at: datetime
    creative_version_id: int
    exclusive: bool
    status: BookingStatus
    revision: int
    approved_by_operator_id: int | None
    approved_at: datetime | None
    activated_by_operator_id: int | None
    activated_at: datetime | None


class PlacementDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligible: bool
    reason_code: Literal[
        "eligible", "disabled", "suspended", "invalid_context", "booking_conflict", "unfilled"
    ]
    booking_id: int | None = None
    booking_revision: int | None = None
    creative_version_id: int | None = None
    creative_revision: int | None = None
    topic: Topic | None = None
    public_name: str | None = None
    website_url: str | None = None
    body_text: str | None = None
    asset_key: str | None = None
    alt_text: str | None = None
    destination_url: str | None = None


def _require_admin(operator: Operator) -> None:
    if operator.disabled_at is not None or operator.role != "admin":
        raise BookingError("administrator access is required")


def _view(row: PlacementBooking) -> BookingView:
    return BookingView(
        id=row.id,
        campaign_id=row.campaign_id,
        surface=row.surface,  # type: ignore[arg-type]
        topic=row.topic,  # type: ignore[arg-type]
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        creative_version_id=row.creative_version_id,
        exclusive=row.exclusive,
        status=row.status,  # type: ignore[arg-type]
        revision=row.revision,
        approved_by_operator_id=row.approved_by_operator_id,
        approved_at=row.approved_at,
        activated_by_operator_id=row.activated_by_operator_id,
        activated_at=row.activated_at,
    )


def _scope_lock(session: Session, surface: str, topic: str) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": f"africasignal:commercial:booking:{surface}:{topic}"},
    )


def _has_eligible_context(
    session: Session, *, topic: str, item_code: str | None, at: datetime
) -> bool:
    query = (
        select(ContentContext, Situation)
        .join(Situation, Situation.current_version_id == ContentContext.assessment_version_id)
        .where(
            ContentContext.suitability == "eligible",
            ContentContext.invalidated_at.is_(None),
            ContentContext.expires_at > at,
            ContentContext.topic_tags.contains([topic]),
            Situation.status == "active",
            Situation.topic == topic,
        )
        .order_by(ContentContext.created_at.desc(), ContentContext.id.desc())
        .limit(100)
    )
    if item_code is not None:
        query = query.where(Situation.item_code == item_code)
    for context, _situation in session.execute(query):
        try:
            decision = classify_content(
                session,
                content_ref=ContextRef(
                    assessment_version_id=context.assessment_version_id,
                    content_hash=context.content_hash,
                ),
                now=at,
            )
        except ContextError:
            continue
        if decision.suitability == "eligible":
            return True
    return False


def _related(
    session: Session, booking: PlacementBooking
) -> tuple[Campaign | None, Sponsor | None, CreativeVersion | None]:
    campaign = session.get(Campaign, booking.campaign_id)
    creative = session.get(CreativeVersion, booking.creative_version_id)
    sponsor = session.get(Sponsor, campaign.sponsor_id) if campaign is not None else None
    return campaign, sponsor, creative


def _validate_approval(
    session: Session, booking: PlacementBooking, *, at: datetime, require_active_window: bool
) -> tuple[Campaign, Sponsor, CreativeVersion]:
    campaign, sponsor, creative = _related(session, booking)
    if campaign is None or sponsor is None or creative is None:
        raise BookingError("booking references are unavailable")
    if campaign.status not in ("approved", "active") or sponsor.status != "approved":
        raise BookingError("approved campaign and sponsor are required")
    if creative.campaign_id != campaign.id or creative.status != "approved":
        raise BookingError("booking requires the campaign's approved creative")
    if booking.starts_at >= booking.ends_at:
        raise BookingError("booking interval is invalid")
    if require_active_window and not (booking.starts_at <= at < booking.ends_at):
        raise BookingError("booking is outside its active interval")
    state = commercial_state(session, at=at)
    if not state["explore_effective"]:
        raise BookingError("Explore sponsorship is not currently effective")
    if publication_suspended(session):
        raise BookingError("publication is suspended")
    if not _has_eligible_context(session, topic=booking.topic, item_code=None, at=at):
        raise BookingError("current eligible context is unavailable")
    return campaign, sponsor, creative


def _has_conflict(session: Session, booking: PlacementBooking) -> bool:
    return (
        session.scalar(
            select(PlacementBooking.id)
            .where(
                PlacementBooking.id != booking.id,
                PlacementBooking.surface == booking.surface,
                PlacementBooking.topic == booking.topic,
                PlacementBooking.exclusive.is_(True),
                PlacementBooking.status == "active",
                PlacementBooking.starts_at < booking.ends_at,
                booking.starts_at < PlacementBooking.ends_at,
            )
            .limit(1)
        )
        is not None
    )


def create_booking(
    session: Session, operator: Operator, campaign_id: int, draft: BookingDraft
) -> BookingView:
    _require_admin(operator)
    campaign = session.get(Campaign, campaign_id)
    creative = session.get(CreativeVersion, draft.creative_version_id)
    if campaign is None:
        raise BookingError("campaign not found")
    if creative is None or creative.campaign_id != campaign.id:
        raise BookingError("creative must belong to the selected campaign")
    row = PlacementBooking(
        campaign_id=campaign.id,
        surface=draft.surface,
        topic=draft.topic,
        starts_at=draft.starts_at,
        ends_at=draft.ends_at,
        creative_version_id=creative.id,
        exclusive=True,
        status="draft",
        revision=1,
        created_by_operator_id=operator.id,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        operator,
        "commercial.booking.create",
        "placement_booking",
        row.id,
        after={
            "campaign_id": row.campaign_id,
            "surface": row.surface,
            "topic": row.topic,
            "starts_at": row.starts_at.isoformat(),
            "ends_at": row.ends_at.isoformat(),
            "creative_version_id": row.creative_version_id,
            "exclusive": True,
            "status": row.status,
            "revision": row.revision,
        },
    )
    return _view(row)


_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"approved", "ended"},
    "approved": {"active", "paused", "ended"},
    "active": {"paused", "ended"},
    "paused": {"approved", "ended"},
    "ended": set(),
}


def transition_booking(
    session: Session,
    operator: Operator,
    booking_id: int,
    *,
    status: Literal["approved", "active", "paused", "ended"],
    expected_revision: int,
    at: datetime | None = None,
) -> BookingView:
    _require_admin(operator)
    moment = at or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("booking transition requires an aware instant")
    moment = moment.astimezone(UTC)
    row = session.scalar(
        select(PlacementBooking).where(PlacementBooking.id == booking_id).with_for_update()
    )
    if row is None:
        raise BookingError("booking not found")
    if row.revision != expected_revision:
        raise BookingError("revision conflict")
    if status not in _TRANSITIONS[row.status]:
        raise BookingError("invalid booking transition")
    if row.status == "active" or status == "active":
        _scope_lock(session, row.surface, row.topic)
    campaign, _, _ = _related(session, row)
    if status in ("approved", "active"):
        campaign, _, _ = _validate_approval(
            session, row, at=moment, require_active_window=status == "active"
        )
    if status == "active" and _has_conflict(session, row):
        raise BookingError("booking conflict")

    before = {"status": row.status, "revision": row.revision}
    row.status = status
    row.revision += 1
    row.updated_at = moment
    if status == "approved":
        row.approved_by_operator_id = operator.id
        row.approved_at = moment
    if status == "active":
        row.activated_by_operator_id = operator.id
        row.activated_at = moment
        assert campaign is not None
        if campaign.status == "approved":
            campaign_before = {"status": campaign.status, "revision": campaign.revision}
            campaign.status = "active"
            campaign.revision += 1
            campaign.updated_at = moment
            session.flush()
            audit.record(
                session,
                operator,
                "commercial.campaign.status",
                "campaign",
                campaign.id,
                before=campaign_before,
                after={"status": campaign.status, "revision": campaign.revision},
            )
    session.flush()
    audit.record(
        session,
        operator,
        "commercial.booking.status",
        "placement_booking",
        row.id,
        before=before,
        after={"status": row.status, "revision": row.revision},
    )
    if status in ("paused", "ended") and campaign is not None and campaign.status == "active":
        other_active = session.scalar(
            select(PlacementBooking.id)
            .where(
                PlacementBooking.campaign_id == campaign.id,
                PlacementBooking.id != row.id,
                PlacementBooking.status == "active",
                PlacementBooking.ends_at > moment,
            )
            .limit(1)
        )
        if other_active is None:
            campaign_before = {"status": campaign.status, "revision": campaign.revision}
            campaign.status = "paused" if status == "paused" else "ended"
            campaign.revision += 1
            campaign.updated_at = moment
            session.flush()
            audit.record(
                session,
                operator,
                "commercial.campaign.status",
                "campaign",
                campaign.id,
                before=campaign_before,
                after={"status": campaign.status, "revision": campaign.revision},
            )
    return _view(row)


def eligible_placement(
    session: Session,
    *,
    surface: Surface,
    topic: Topic,
    item_code: str | None,
    at: datetime,
) -> PlacementDecision:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("placement time must be timezone-aware")
    moment = at.astimezone(UTC)
    if publication_suspended(session):
        return PlacementDecision(eligible=False, reason_code="suspended")
    try:
        state = commercial_state(session, at=moment)
    except Exception:
        return PlacementDecision(eligible=False, reason_code="disabled")
    if not state.get("explore_effective"):
        return PlacementDecision(eligible=False, reason_code="disabled")
    if not _has_eligible_context(session, topic=topic, item_code=item_code, at=moment):
        return PlacementDecision(eligible=False, reason_code="invalid_context")
    rows = session.scalars(
        select(PlacementBooking)
        .where(
            PlacementBooking.surface == surface,
            PlacementBooking.topic == topic,
            PlacementBooking.status == "active",
            PlacementBooking.starts_at <= moment,
            PlacementBooking.ends_at > moment,
        )
        .order_by(PlacementBooking.id)
        .limit(2)
    ).all()
    if len(rows) > 1:
        return PlacementDecision(eligible=False, reason_code="booking_conflict")
    if not rows:
        return PlacementDecision(eligible=False, reason_code="unfilled")
    booking = rows[0]
    campaign, sponsor, creative = _related(session, booking)
    if (
        campaign is None
        or campaign.status != "active"
        or sponsor is None
        or sponsor.status != "approved"
        or creative is None
        or creative.status != "approved"
        or creative.campaign_id != campaign.id
    ):
        return PlacementDecision(eligible=False, reason_code="invalid_context")
    return PlacementDecision(
        eligible=True,
        reason_code="eligible",
        booking_id=booking.id,
        booking_revision=booking.revision,
        creative_version_id=creative.id,
        creative_revision=creative.revision,
        topic=booking.topic,  # type: ignore[arg-type]
        public_name=sponsor.public_name,
        website_url=sponsor.website_url,
        body_text=creative.body_text,
        asset_key=creative.asset_key,
        alt_text=creative.alt_text,
        destination_url=creative.destination_url,
    )
