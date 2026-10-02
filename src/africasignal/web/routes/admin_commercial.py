"""Admin-only first-party commercial sponsor, campaign, creative, and booking console."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy import func, select

from africasignal.models import Campaign, CreativeVersion, Job, PlacementBooking
from africasignal.operations import (
    commercial_bookings,
    commercial_campaigns,
    commercial_controls,
    commercial_packages,
)
from africasignal.operations.commercial_bookings import BookingDraft
from africasignal.operations.commercial_campaigns import (
    CampaignDraft,
    CreativeDraft,
    create_campaign,
)
from africasignal.operations.commercial_sponsors import (
    SponsorDraft,
    create_sponsor,
    list_sponsors,
)
from africasignal.web.deps import AdminOperator, Authenticated, DbSession
from africasignal.web.routes.admin import _page, _redirect, templates

router = APIRouter(prefix="/admin")
LAGOS = ZoneInfo("Africa/Lagos")
MAX_FORM_FIELDS = 12


def _lagos(value: datetime | None) -> str:
    return value.astimezone(LAGOS).strftime("%Y-%m-%d %H:%M WAT") if value else "—"


templates.env.filters["lagos"] = _lagos


async def _read_form(
    request: Request,
    *,
    allowed: frozenset[str],
    required: frozenset[str],
) -> dict[str, str]:
    """Reject duplicate, unknown, file, and oversized field-count inputs."""
    form = await request.form()
    pairs = list(form.multi_items())
    if len(pairs) > MAX_FORM_FIELDS:
        raise ValueError("too many form fields")
    keys = {key for key, _value in pairs}
    if keys - allowed or required - keys or len(keys) != len(pairs):
        raise ValueError("form fields are invalid")
    values: dict[str, str] = {}
    for key, value in pairs:
        if not isinstance(value, str) or len(value) > 8_192:
            raise ValueError("form fields are invalid")
        values[key] = value
    return values


def _integer(value: str, *, minimum: int = 1) -> int:
    parsed = int(value)
    if parsed < minimum:
        raise ValueError("integer value is outside the allowed range")
    return parsed


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("booking times must include a UTC offset")
    return parsed.astimezone(UTC)


def _page_view(
    request: Request,
    db: Any,
    auth: Authenticated,
    *,
    status_code: int = 200,
    error: str | None = None,
) -> Response:
    sponsors = list_sponsors(db)
    sponsor_by_id = {row.id: row.public_name for row in sponsors}
    sponsor_category_by_id = {row.id: row.category for row in sponsors}
    campaigns = list(
        db.scalars(
            select(Campaign).order_by(Campaign.created_at.desc(), Campaign.id.desc()).limit(200)
        )
    )
    creatives = list(
        db.scalars(
            select(CreativeVersion)
            .order_by(CreativeVersion.created_at.desc(), CreativeVersion.id.desc())
            .limit(300)
        )
    )
    bookings = list(
        db.scalars(
            select(PlacementBooking)
            .order_by(PlacementBooking.created_at.desc(), PlacementBooking.id.desc())
            .limit(300)
        )
    )
    package_drafts = commercial_packages.list_package_drafts(db)
    context_jobs = dict(
        db.execute(
            select(Job.status, func.count())
            .where(Job.kind == "commercial_context_rebuild")
            .group_by(Job.status)
        ).all()
    )
    return _page(
        request,
        "admin/commercial.html",
        auth,
        status_code,
        error=error,
        sponsors=sponsors,
        sponsor_by_id=sponsor_by_id,
        sponsor_category_by_id=sponsor_category_by_id,
        campaigns=campaigns,
        creatives=creatives,
        bookings=bookings,
        package_drafts=package_drafts,
        context_jobs=context_jobs,
        controls=commercial_controls.current(db),
    )


def _mutation_error(
    request: Request,
    db: Any,
    auth: Authenticated,
    exc: Exception,
) -> Response:
    db.rollback()
    if isinstance(exc, ValidationError):
        message, status_code = "Submitted commercial data is invalid.", 400
    else:
        message = str(exc)
        status_code = 409 if "revision conflict" in message.casefold() else 400
    return _page_view(request, db, auth, status_code=status_code, error=message)


@router.get("/commercial")
def commercial_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _page_view(request, db, auth)


@router.post("/commercial/context/rebuild")
def context_rebuild(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    from africasignal.operations.commercial_invalidation import enqueue_context_scan

    queued = enqueue_context_scan(db, now=datetime.now(UTC))
    if queued is None:
        return _page_view(
            request,
            db,
            auth,
            status_code=409,
            error=(
                "Enable the global commercial and workload switches before queueing "
                "a context rebuild."
            ),
        )
    return _redirect("/admin/commercial", auth)


@router.post("/commercial/packages")
async def package_create(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    try:
        fields = await _read_form(
            request,
            allowed=frozenset(
                {
                    "campaign_id",
                    "topic",
                    "starts_at",
                    "ends_at",
                    "expected_campaign_revision",
                    "expected_controls_revision",
                }
            ),
            required=frozenset(
                {
                    "campaign_id",
                    "topic",
                    "starts_at",
                    "ends_at",
                    "expected_campaign_revision",
                    "expected_controls_revision",
                }
            ),
        )
        if fields["topic"] not in ("energy", "food"):
            raise ValueError("topic must be energy or food")
        commercial_packages.create_package_draft(
            db,
            auth.operator,
            commercial_packages.PackageRequest(
                campaign_id=_integer(fields["campaign_id"]),
                topic=cast(Literal["energy", "food"], fields["topic"]),
                starts_at=_instant(fields["starts_at"]),
                ends_at=_instant(fields["ends_at"]),
                expected_campaign_revision=_integer(fields["expected_campaign_revision"]),
                expected_controls_revision=_integer(fields["expected_controls_revision"]),
            ),
        )
    except (ValueError, TypeError, ValidationError, PermissionError) as exc:
        return _mutation_error(request, db, auth, exc)
    return _redirect("/admin/commercial", auth)


@router.post("/commercial/packages/{draft_id}/review")
async def package_review(
    draft_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    try:
        fields = await _read_form(
            request,
            allowed=frozenset({"status", "expected_revision"}),
            required=frozenset({"status", "expected_revision"}),
        )
        if fields["status"] not in ("approved", "rejected"):
            raise ValueError("invalid package decision")
        commercial_packages.review_package_draft(
            db,
            auth.operator,
            draft_id,
            status=cast(Literal["approved", "rejected"], fields["status"]),
            expected_revision=_integer(fields["expected_revision"]),
        )
    except (ValueError, TypeError, ValidationError, PermissionError) as exc:
        return _mutation_error(request, db, auth, exc)
    return _redirect("/admin/commercial", auth)


@router.post("/commercial/sponsors")
async def sponsor_create(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    try:
        fields = await _read_form(
            request,
            allowed=frozenset({"public_name", "website_url", "contact_email", "category"}),
            required=frozenset({"public_name", "website_url", "contact_email"}),
        )
        draft = SponsorDraft.model_validate(fields)
        create_sponsor(db, auth.operator, draft)
    except (ValueError, TypeError, ValidationError, PermissionError) as exc:
        return _mutation_error(request, db, auth, exc)
    return _redirect("/admin/commercial", auth)


@router.post("/commercial/sponsors/{sponsor_id}/status")
async def sponsor_status(
    sponsor_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    try:
        fields = await _read_form(
            request,
            allowed=frozenset({"status", "expected_revision"}),
            required=frozenset({"status", "expected_revision"}),
        )
        status = fields["status"]
        if status not in ("approved", "paused", "retired"):
            raise ValueError("invalid sponsor status")
        commercial_campaigns.transition_sponsor(
            db,
            auth.operator,
            sponsor_id,
            status=cast(Literal["approved", "paused", "retired"], status),
            expected_revision=_integer(fields["expected_revision"]),
        )
    except (ValueError, TypeError, ValidationError, PermissionError) as exc:
        return _mutation_error(request, db, auth, exc)
    return _redirect("/admin/commercial", auth)


@router.post("/commercial/campaigns")
async def campaign_create(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    try:
        fields = await _read_form(
            request,
            allowed=frozenset(
                {"sponsor_id", "internal_name", "agreed_fee_minor", "agreement_reference"}
            ),
            required=frozenset({"sponsor_id", "internal_name"}),
        )
        fee = fields.get("agreed_fee_minor", "").strip()
        create_campaign(
            db,
            auth.operator,
            CampaignDraft(
                sponsor_id=_integer(fields["sponsor_id"]),
                internal_name=fields["internal_name"],
                agreed_fee_minor=int(fee) if fee else None,
                agreement_reference=fields.get("agreement_reference") or None,
            ),
        )
    except (ValueError, TypeError, ValidationError, PermissionError) as exc:
        return _mutation_error(request, db, auth, exc)
    return _redirect("/admin/commercial", auth)


@router.post("/commercial/campaigns/{campaign_id}/status")
async def campaign_status(
    campaign_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    try:
        fields = await _read_form(
            request,
            allowed=frozenset({"status", "expected_revision"}),
            required=frozenset({"status", "expected_revision"}),
        )
        status = fields["status"]
        if status not in ("approved", "active", "paused", "ended"):
            raise ValueError("invalid campaign status")
        commercial_campaigns.transition_campaign(
            db,
            auth.operator,
            campaign_id,
            status=cast(Literal["approved", "active", "paused", "ended"], status),
            expected_revision=_integer(fields["expected_revision"]),
        )
    except (ValueError, TypeError, ValidationError, PermissionError) as exc:
        return _mutation_error(request, db, auth, exc)
    return _redirect("/admin/commercial", auth)


@router.post("/commercial/campaigns/{campaign_id}/creatives")
async def creative_create(
    campaign_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    try:
        fields = await _read_form(
            request,
            allowed=frozenset({"body_text", "destination_url"}),
            required=frozenset({"body_text", "destination_url"}),
        )
        commercial_campaigns.create_creative_version(
            db,
            auth.operator,
            campaign_id,
            CreativeDraft(
                body_text=fields["body_text"],
                destination_url=fields["destination_url"],
            ),
        )
    except (ValueError, TypeError, ValidationError, PermissionError) as exc:
        return _mutation_error(request, db, auth, exc)
    return _redirect("/admin/commercial", auth)


@router.post("/commercial/creatives/{creative_id}/review")
async def creative_review(
    creative_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    try:
        fields = await _read_form(
            request,
            allowed=frozenset({"status", "expected_revision"}),
            required=frozenset({"status", "expected_revision"}),
        )
        status = fields["status"]
        if status not in ("approved", "rejected", "withdrawn"):
            raise ValueError("invalid creative status")
        commercial_campaigns.review_creative(
            db,
            auth.operator,
            creative_id,
            status=cast(Literal["approved", "rejected", "withdrawn"], status),
            expected_revision=_integer(fields["expected_revision"]),
        )
    except (ValueError, TypeError, ValidationError, PermissionError) as exc:
        return _mutation_error(request, db, auth, exc)
    return _redirect("/admin/commercial", auth)


@router.post("/commercial/bookings")
async def booking_create(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    try:
        fields = await _read_form(
            request,
            allowed=frozenset(
                {"campaign_id", "topic", "starts_at", "ends_at", "creative_version_id"}
            ),
            required=frozenset(
                {"campaign_id", "topic", "starts_at", "ends_at", "creative_version_id"}
            ),
        )
        if fields["topic"] not in ("energy", "food"):
            raise ValueError("topic must be energy or food")
        commercial_bookings.create_booking(
            db,
            auth.operator,
            _integer(fields["campaign_id"]),
            BookingDraft(
                topic=cast(Literal["energy", "food"], fields["topic"]),
                starts_at=_instant(fields["starts_at"]),
                ends_at=_instant(fields["ends_at"]),
                creative_version_id=_integer(fields["creative_version_id"]),
            ),
        )
    except (ValueError, TypeError, ValidationError, PermissionError) as exc:
        return _mutation_error(request, db, auth, exc)
    return _redirect("/admin/commercial", auth)


@router.post("/commercial/bookings/{booking_id}/status")
async def booking_status(
    booking_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    try:
        fields = await _read_form(
            request,
            allowed=frozenset({"status", "expected_revision"}),
            required=frozenset({"status", "expected_revision"}),
        )
        status = fields["status"]
        if status not in ("approved", "active", "paused", "ended"):
            raise ValueError("invalid booking status")
        commercial_bookings.transition_booking(
            db,
            auth.operator,
            booking_id,
            status=cast(Literal["approved", "active", "paused", "ended"], status),
            expected_revision=_integer(fields["expected_revision"]),
        )
    except (ValueError, TypeError, ValidationError, PermissionError) as exc:
        return _mutation_error(request, db, auth, exc)
    return _redirect("/admin/commercial", auth)
