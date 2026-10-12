"""Revisioned sponsor, campaign and creative lifecycle services."""

from __future__ import annotations

import ipaddress
import re
from datetime import UTC, datetime
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.models import Campaign, CreativeVersion, Operator, PlacementBooking, Sponsor

CampaignStatus = Literal["draft", "approved", "active", "paused", "ended"]
CreativeStatus = Literal["draft", "approved", "rejected", "withdrawn"]
ASSET_KEY_RE = re.compile(r"^commercial/creative/[a-f0-9]{64}\.(?:png|jpg|webp)$")


class CommercialLifecycleError(ValueError):
    """Safe domain error for commercial lifecycle operations."""


class CampaignDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    sponsor_id: int = Field(ge=1)
    internal_name: str = Field(min_length=1, max_length=100)
    agreed_fee_minor: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    agreement_reference: str | None = Field(default=None, max_length=200)

    @field_validator("internal_name", "agreement_reference")
    @classmethod
    def no_controls(cls, value: str | None) -> str | None:
        if value is not None and any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("text must not contain control characters")
        return value


class CreativeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    body_text: str = Field(min_length=1, max_length=1000)
    destination_url: str = Field(min_length=12, max_length=500)
    asset_key: str | None = Field(default=None, max_length=200)
    alt_text: str | None = Field(default=None, max_length=300)

    @field_validator("body_text", "alt_text")
    @classmethod
    def plain_bounded_text(cls, value: str | None) -> str | None:
        if value is not None and any(ord(char) < 32 and char not in "\t\n" for char in value):
            raise ValueError("text must not contain control characters")
        return value

    @field_validator("destination_url")
    @classmethod
    def valid_destination(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme.lower() != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("destination must be HTTPS without credentials, query or fragment")
        hostname = parts.hostname.rstrip(".").lower()
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("destination host must use public DNS") from None
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError("destination must use a public DNS name")
        if (
            hostname == "localhost"
            or hostname.endswith((".localhost", ".local", ".internal", ".test", ".example"))
            or "." not in hostname
        ):
            raise ValueError("destination must use a public DNS name")
        try:
            port = parts.port
        except ValueError:
            raise ValueError("destination port is invalid") from None
        if port not in (None, 443):
            raise ValueError("destination must use standard HTTPS")
        return urlunsplit(("https", hostname, parts.path.rstrip("/"), "", ""))

    @field_validator("asset_key")
    @classmethod
    def local_asset_only(cls, value: str | None) -> str | None:
        if value is not None and not ASSET_KEY_RE.fullmatch(value):
            raise ValueError("asset key must reference a reviewed local image")
        return value

    @field_validator("alt_text")
    @classmethod
    def alt_for_asset(cls, value: str | None, info) -> str | None:  # type: ignore[no-untyped-def]
        if info.data.get("asset_key") is not None and not value:
            raise ValueError("local image requires alternative text")
        return value


class CampaignView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    sponsor_id: int
    internal_name: str
    status: CampaignStatus
    revision: int
    currency: Literal["NGN"]
    agreed_fee_minor: int | None
    agreement_reference: str | None
    approved_by_operator_id: int | None
    approved_at: datetime | None


class CreativeView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    campaign_id: int
    version: int
    revision: int
    body_text: str
    asset_key: str | None
    alt_text: str | None
    destination_url: str
    status: CreativeStatus
    reviewer_operator_id: int | None
    reviewed_at: datetime | None


def _require_admin(operator: Operator) -> None:
    if operator.role != "admin" or operator.disabled_at is not None:
        raise CommercialLifecycleError("administrator access is required")


def _campaign_view(row: Campaign) -> CampaignView:
    return CampaignView(
        id=row.id,
        sponsor_id=row.sponsor_id,
        internal_name=row.internal_name,
        status=cast(CampaignStatus, row.status),
        revision=row.revision,
        currency="NGN",
        agreed_fee_minor=row.agreed_fee_minor,
        agreement_reference=row.agreement_reference,
        approved_by_operator_id=row.approved_by_operator_id,
        approved_at=row.approved_at,
    )


def _creative_view(row: CreativeVersion) -> CreativeView:
    return CreativeView(
        id=row.id,
        campaign_id=row.campaign_id,
        version=row.version,
        revision=row.revision,
        body_text=row.body_text,
        asset_key=row.asset_key,
        alt_text=row.alt_text,
        destination_url=row.destination_url,
        status=cast(CreativeStatus, row.status),
        reviewer_operator_id=row.reviewer_operator_id,
        reviewed_at=row.reviewed_at,
    )


def create_campaign(session: Session, operator: Operator, draft: CampaignDraft) -> CampaignView:
    _require_admin(operator)
    sponsor = session.get(Sponsor, draft.sponsor_id)
    if sponsor is None:
        raise CommercialLifecycleError("sponsor not found")
    row = Campaign(
        sponsor_id=sponsor.id,
        internal_name=draft.internal_name,
        status="draft",
        revision=1,
        currency="NGN",
        agreed_fee_minor=draft.agreed_fee_minor,
        agreement_reference=draft.agreement_reference,
        created_by_operator_id=operator.id,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        operator,
        "commercial.campaign.create",
        "campaign",
        row.id,
        after={
            "sponsor_id": sponsor.id,
            "internal_name": row.internal_name,
            "status": row.status,
            "revision": row.revision,
            "currency": row.currency,
            "agreed_fee_minor": row.agreed_fee_minor,
        },
    )
    return _campaign_view(row)


def update_campaign_terms(
    session: Session,
    operator: Operator,
    campaign_id: int,
    *,
    internal_name: str,
    agreed_fee_minor: int | None,
    agreement_reference: str | None,
    expected_revision: int,
) -> CampaignView:
    _require_admin(operator)
    row = session.scalar(select(Campaign).where(Campaign.id == campaign_id).with_for_update())
    if row is None:
        raise CommercialLifecycleError("campaign not found")
    if row.revision != expected_revision:
        raise CommercialLifecycleError("revision conflict")
    if row.status in ("active", "ended"):
        raise CommercialLifecycleError("campaign terms cannot change in this state")
    draft = CampaignDraft(
        sponsor_id=row.sponsor_id,
        internal_name=internal_name,
        agreed_fee_minor=agreed_fee_minor,
        agreement_reference=agreement_reference,
    )
    before = {
        "status": row.status,
        "revision": row.revision,
        "agreed_fee_minor": row.agreed_fee_minor,
    }
    row.internal_name = draft.internal_name
    row.agreed_fee_minor = draft.agreed_fee_minor
    row.agreement_reference = draft.agreement_reference
    if row.status != "draft":
        row.status = "draft"
        row.approved_by_operator_id = None
        row.approved_at = None
    row.revision += 1
    row.updated_at = datetime.now(UTC)
    session.flush()
    audit.record(
        session,
        operator,
        "commercial.campaign.terms_update",
        "campaign",
        row.id,
        before=before,
        after={
            "status": row.status,
            "revision": row.revision,
            "agreed_fee_minor": row.agreed_fee_minor,
        },
    )
    return _campaign_view(row)


_CAMPAIGN_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"approved", "ended"},
    "approved": {"active", "paused", "ended"},
    "active": {"paused", "ended"},
    "paused": {"approved", "ended"},
    "ended": set(),
}
_SPONSOR_TRANSITIONS: dict[str, set[str]] = {
    "pending_review": {"approved", "retired"},
    "approved": {"paused", "retired"},
    "paused": {"approved", "retired"},
    "retired": set(),
}


def transition_sponsor(
    session: Session,
    operator: Operator,
    sponsor_id: int,
    *,
    status: Literal["approved", "paused", "retired"],
    expected_revision: int,
) -> Sponsor:
    _require_admin(operator)
    row = session.scalar(select(Sponsor).where(Sponsor.id == sponsor_id).with_for_update())
    if row is None:
        raise CommercialLifecycleError("sponsor not found")
    if row.revision != expected_revision:
        raise CommercialLifecycleError("revision conflict")
    if status not in _SPONSOR_TRANSITIONS[row.status]:
        raise CommercialLifecycleError("invalid sponsor transition")
    before = {"status": row.status, "revision": row.revision}
    row.status = status
    row.revision += 1
    row.updated_by_operator_id = operator.id
    row.updated_at = datetime.now(UTC)
    session.flush()
    audit.record(
        session,
        operator,
        "commercial.sponsor.status",
        "sponsor",
        row.id,
        before=before,
        after={"status": row.status, "revision": row.revision},
    )
    return row


def transition_campaign(
    session: Session,
    operator: Operator,
    campaign_id: int,
    *,
    status: Literal["approved", "active", "paused", "ended"],
    expected_revision: int,
) -> CampaignView:
    _require_admin(operator)
    row = session.scalar(select(Campaign).where(Campaign.id == campaign_id).with_for_update())
    if row is None:
        raise CommercialLifecycleError("campaign not found")
    if row.revision != expected_revision:
        raise CommercialLifecycleError("revision conflict")
    if status not in _CAMPAIGN_TRANSITIONS[row.status]:
        raise CommercialLifecycleError("invalid campaign transition")
    sponsor = session.get(Sponsor, row.sponsor_id)
    if status in ("approved", "active") and (sponsor is None or sponsor.status != "approved"):
        raise CommercialLifecycleError("sponsor must be approved")
    if status == "active":
        has_approved = session.scalar(
            select(CreativeVersion.id).where(
                CreativeVersion.campaign_id == row.id, CreativeVersion.status == "approved"
            )
        )
        if has_approved is None:
            raise CommercialLifecycleError("campaign requires an approved creative")
        has_booking = session.scalar(
            select(PlacementBooking.id)
            .where(PlacementBooking.campaign_id == row.id, PlacementBooking.status == "active")
            .limit(1)
        )
        if has_booking is None:
            raise CommercialLifecycleError(
                "campaign activation requires an eligible active booking"
            )
    before = {"status": row.status, "revision": row.revision}
    row.status = status
    row.revision += 1
    row.updated_at = datetime.now(UTC)
    if status == "approved":
        row.approved_by_operator_id = operator.id
        row.approved_at = datetime.now(UTC)
    session.flush()
    audit.record(
        session,
        operator,
        "commercial.campaign.status",
        "campaign",
        row.id,
        before=before,
        after={"status": row.status, "revision": row.revision},
    )
    return _campaign_view(row)


def create_creative_version(
    session: Session,
    operator: Operator,
    campaign_id: int,
    draft: CreativeDraft,
) -> CreativeView:
    _require_admin(operator)
    campaign = session.scalar(select(Campaign).where(Campaign.id == campaign_id).with_for_update())
    if campaign is None:
        raise CommercialLifecycleError("campaign not found")
    if campaign.status == "ended":
        raise CommercialLifecycleError("campaign has ended")
    last_version = session.scalar(
        select(func.coalesce(func.max(CreativeVersion.version), 0)).where(
            CreativeVersion.campaign_id == campaign.id
        )
    )
    version = int(last_version or 0) + 1
    row = CreativeVersion(
        campaign_id=campaign.id,
        version=version,
        revision=1,
        body_text=draft.body_text,
        asset_key=draft.asset_key,
        alt_text=draft.alt_text,
        destination_url=draft.destination_url,
        status="draft",
        created_by_operator_id=operator.id,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        operator,
        "commercial.creative.create",
        "creative_version",
        row.id,
        after={"campaign_id": campaign.id, "version": version, "status": row.status},
    )
    return _creative_view(row)


def review_creative(
    session: Session,
    operator: Operator,
    creative_id: int,
    *,
    status: Literal["approved", "rejected", "withdrawn"],
    expected_revision: int,
) -> CreativeView:
    _require_admin(operator)
    row = session.scalar(
        select(CreativeVersion).where(CreativeVersion.id == creative_id).with_for_update()
    )
    if row is None:
        raise CommercialLifecycleError("creative not found")
    if row.revision != expected_revision:
        raise CommercialLifecycleError("revision conflict")
    if row.status != "draft" and not (row.status == "approved" and status == "withdrawn"):
        raise CommercialLifecycleError("invalid creative transition")
    campaign = session.get(Campaign, row.campaign_id)
    sponsor = session.get(Sponsor, campaign.sponsor_id) if campaign is not None else None
    if status == "approved" and (
        campaign is None
        or campaign.status not in ("approved", "active", "paused")
        or sponsor is None
        or sponsor.status != "approved"
    ):
        raise CommercialLifecycleError("approved campaign and sponsor are required")
    before = {"status": row.status, "revision": row.revision}
    row.status = status
    row.revision += 1
    row.reviewer_operator_id = operator.id
    row.reviewed_at = datetime.now(UTC)
    session.flush()
    audit.record(
        session,
        operator,
        "commercial.creative.review",
        "creative_version",
        row.id,
        before=before,
        after={"status": row.status, "revision": row.revision, "version": row.version},
    )
    return _creative_view(row)
