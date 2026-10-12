"""Admin-only sponsor records for the first-party sponsorship pilot."""

from __future__ import annotations

import ipaddress
import re
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.models import Operator, Sponsor

SponsorStatus = Literal["pending_review", "approved", "paused", "retired"]
SponsorCategory = Literal[
    "energy_provider",
    "energy_efficiency",
    "food_retailer",
    "agriculture",
    "general_business",
    "unclassified",
]
EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


class SponsorError(ValueError):
    """Safe domain error for sponsor administration."""


class SponsorDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    public_name: str = Field(min_length=1, max_length=100)
    website_url: str = Field(min_length=12, max_length=500)
    contact_email: str = Field(min_length=3, max_length=254)
    category: SponsorCategory = "unclassified"

    @field_validator("public_name")
    @classmethod
    def valid_public_name(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("public name must not contain control characters")
        return value

    @field_validator("website_url")
    @classmethod
    def valid_website(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme.lower() != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("website must be an HTTPS URL without credentials, query or fragment")
        hostname = parts.hostname.rstrip(".").lower()
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("website host must be a valid public DNS name") from None
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError("website must use a public DNS name, not an IP address")
        if (
            hostname == "localhost"
            or hostname.endswith((".localhost", ".local", ".internal", ".test", ".example"))
            or "." not in hostname
        ):
            raise ValueError("website must use a public DNS name")
        try:
            port = parts.port
        except ValueError:
            raise ValueError("website port is invalid") from None
        if port not in (None, 443):
            raise ValueError("website must use the standard HTTPS port")
        return urlunsplit(("https", hostname, parts.path.rstrip("/"), "", ""))

    @field_validator("contact_email")
    @classmethod
    def valid_contact_email(cls, value: str) -> str:
        if not EMAIL_RE.fullmatch(value):
            raise ValueError("contact email is invalid")
        return value.lower()


class SponsorView(BaseModel):
    """Immutable admin projection. Never use this projection in a public response."""

    model_config = ConfigDict(frozen=True)

    id: int
    public_name: str
    website_url: str
    contact_email: str
    category: SponsorCategory
    status: SponsorStatus
    revision: int
    created_at: datetime
    updated_at: datetime
    created_by_operator_id: int
    updated_by_operator_id: int


def _view(row: Sponsor) -> SponsorView:
    return SponsorView(
        id=row.id,
        public_name=row.public_name,
        website_url=row.website_url,
        contact_email=row.contact_email,
        category=row.category,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by_operator_id=row.created_by_operator_id,
        updated_by_operator_id=row.updated_by_operator_id,
    )


def create_sponsor(session: Session, operator: Operator, draft: SponsorDraft) -> SponsorView:
    """Create a pending-review record; caller owns commit/rollback and admin authorization."""
    if operator.role != "admin" or operator.disabled_at is not None:
        raise SponsorError("administrator access is required")
    now = datetime.now(UTC)
    row = Sponsor(
        public_name=draft.public_name,
        website_url=draft.website_url,
        contact_email=draft.contact_email,
        category=draft.category,
        status="pending_review",
        revision=1,
        created_by_operator_id=operator.id,
        updated_by_operator_id=operator.id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        operator,
        "commercial.sponsor.create",
        "sponsor",
        row.id,
        after={
            "public_name": row.public_name,
            "website_url": row.website_url,
            "category": row.category,
            "status": row.status,
            "revision": row.revision,
        },
    )
    return _view(row)


def get_sponsor(session: Session, sponsor_id: int) -> SponsorView:
    row = session.get(Sponsor, sponsor_id)
    if row is None:
        raise SponsorError("sponsor not found")
    return _view(row)


def list_sponsors(
    session: Session, *, status: SponsorStatus | None = None, limit: int = 100
) -> tuple[SponsorView, ...]:
    if not 1 <= limit <= 200:
        raise SponsorError("limit must be between 1 and 200")
    query = select(Sponsor).order_by(Sponsor.created_at.desc(), Sponsor.id.desc()).limit(limit)
    if status is not None:
        query = query.where(Sponsor.status == status)
    return tuple(_view(row) for row in session.scalars(query))
