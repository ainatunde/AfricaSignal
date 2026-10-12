"""Revision-bound package drafts based on current eligible contexts and known manual fees."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.models import (
    Campaign,
    CommercialControl,
    CommercialDraft,
    ContentContext,
    Operator,
    PlacementBooking,
    Situation,
    Sponsor,
)
from africasignal.operations.commercial_context import (
    CLASSIFIER_VERSION,
    TAXONOMY_VERSION,
    ContextError,
    classify_content,
)
from africasignal.operations.commercial_context_contract import ContextRef
from africasignal.operations.commercial_matching import CATEGORY_TOPICS, Topic


class PackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: int = Field(ge=1)
    topic: Topic
    starts_at: datetime
    ends_at: datetime
    expected_campaign_revision: int = Field(ge=1)
    expected_controls_revision: int = Field(ge=1)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("package interval must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bounded_interval(self) -> PackageRequest:
        if self.starts_at >= self.ends_at:
            raise ValueError("package end must follow its start")
        if self.ends_at - self.starts_at > timedelta(days=90):
            raise ValueError("package interval cannot exceed 90 days")
        return self


class PackageDraftView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    campaign_id: int
    status: str
    revision: int
    input_hash: str
    input_refs: dict[str, object]
    facts: dict[str, object]
    reason_codes: tuple[str, ...]
    created_at: datetime
    reviewed_by_operator_id: int | None
    reviewed_at: datetime | None


class PackageError(ValueError):
    """Safe refusal for stale or invalid package proposals."""


def _admin(operator: Operator) -> None:
    if operator.disabled_at is not None or operator.role != "admin":
        raise PackageError("administrator access is required")


def _view(row: CommercialDraft) -> PackageDraftView:
    return PackageDraftView(
        id=row.id,
        campaign_id=row.campaign_id,
        status=row.status,
        revision=row.revision,
        input_hash=row.input_hash,
        input_refs=row.input_refs,
        facts=row.facts,
        reason_codes=tuple(row.reason_codes),
        created_at=row.created_at,
        reviewed_by_operator_id=row.reviewed_by_operator_id,
        reviewed_at=row.reviewed_at,
    )


def _eligible_contexts(
    session: Session, *, topic: Topic, at: datetime, limit: int = 101
) -> list[dict[str, object]]:
    rows = session.scalars(
        select(ContentContext)
        .join(Situation, Situation.current_version_id == ContentContext.assessment_version_id)
        .where(
            ContentContext.suitability == "eligible",
            ContentContext.invalidated_at.is_(None),
            ContentContext.expires_at > at,
            ContentContext.taxonomy_version == TAXONOMY_VERSION,
            ContentContext.classifier_version == CLASSIFIER_VERSION,
            ContentContext.topic_tags.contains([topic]),
            Situation.status == "active",
            Situation.topic == topic,
        )
        .order_by(ContentContext.id)
        .limit(limit)
    ).all()
    eligible: list[dict[str, object]] = []
    for row in rows:
        ref = ContextRef(
            assessment_version_id=row.assessment_version_id,
            content_hash=row.content_hash,
        )
        try:
            decision = classify_content(session, content_ref=ref, now=at)
        except ContextError:
            continue
        if decision.suitability == "eligible":
            eligible.append(
                {
                    "context_id": row.id,
                    "context_revision": row.revision,
                    "assessment_version_id": row.assessment_version_id,
                    "content_hash": row.content_hash,
                }
            )
    return eligible


def _conflicts(
    session: Session, *, topic: Topic, starts_at: datetime, ends_at: datetime
) -> list[dict[str, int]]:
    rows = session.scalars(
        select(PlacementBooking)
        .where(
            PlacementBooking.surface == "explore_topic",
            PlacementBooking.topic == topic,
            PlacementBooking.status.in_(("approved", "active", "paused")),
            PlacementBooking.starts_at < ends_at,
            starts_at < PlacementBooking.ends_at,
        )
        .order_by(PlacementBooking.id)
        .limit(101)
    ).all()
    return [{"booking_id": row.id, "revision": row.revision} for row in rows]


def _canonical_hash(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_package_draft(
    session: Session,
    operator: Operator,
    request: PackageRequest,
    *,
    at: datetime | None = None,
) -> PackageDraftView:
    """Create an idempotent proposal snapshot. It never creates/reserves a placement booking."""
    _admin(operator)
    moment = at or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("package draft time must be timezone-aware")
    moment = moment.astimezone(UTC)
    control = session.scalar(
        select(CommercialControl)
        .where(CommercialControl.singleton_id == 1)
        .with_for_update(read=True)
    )
    if control is None or control.revision != request.expected_controls_revision:
        raise PackageError("commercial controls revision conflict; reload before drafting")
    campaign = session.scalar(
        select(Campaign).where(Campaign.id == request.campaign_id).with_for_update()
    )
    if campaign is None:
        raise PackageError("campaign not found")
    if campaign.revision != request.expected_campaign_revision:
        raise PackageError("campaign revision conflict; reload before drafting")
    sponsor = session.get(Sponsor, campaign.sponsor_id)
    if (
        campaign.status not in ("approved", "active")
        or sponsor is None
        or sponsor.status != "approved"
    ):
        raise PackageError("an approved sponsor and campaign are required")
    if request.topic not in CATEGORY_TOPICS.get(sponsor.category, frozenset()):
        raise PackageError("campaign sponsor category does not match the requested topic")

    contexts = _eligible_contexts(session, topic=request.topic, at=moment)
    conflicts = _conflicts(
        session,
        topic=request.topic,
        starts_at=request.starts_at,
        ends_at=request.ends_at,
    )
    capped = len(contexts) > 100
    context_snapshot = contexts[:100]
    conflict_snapshot = conflicts[:100]
    inventory_status = (
        "conflict"
        if conflict_snapshot
        else "unknown_snapshot_capped"
        if capped
        else "available_at_draft_time"
        if context_snapshot
        else "unknown_no_current_eligible_content"
    )
    fee_known = campaign.agreed_fee_minor is not None
    reason_codes = [
        "eligible_current_content_present" if context_snapshot else "no_current_eligible_content",
        "eligible_context_snapshot_capped" if capped else "eligible_context_snapshot_bounded",
        "overlapping_booking" if conflict_snapshot else "no_overlapping_booking",
        f"curated_category_{sponsor.category}",
        "operator_review_required",
        "agreed_fee_known" if fee_known else "agreed_fee_unknown",
    ]
    facts: dict[str, object] = {
        "surface": "explore_topic",
        "topic": request.topic,
        "starts_at": request.starts_at.isoformat(),
        "ends_at": request.ends_at.isoformat(),
        "inventory_status": inventory_status,
        "inventory_basis": (
            "one exclusive topic module; eligibility is checked at draft time only; "
            "future content, delivery, traffic, and fill are not guaranteed"
        ),
        "eligible_context_count": min(len(contexts), 100),
        "eligible_context_count_is_lower_bound": capped,
        "booking_conflicts": conflict_snapshot,
        "price_status": "known" if fee_known else "unknown",
        "currency": campaign.currency,
        "agreed_fee_minor": campaign.agreed_fee_minor,
        "fee_basis": "operator-entered manual agreement; no payment or settlement",
        "drafted_at": moment.isoformat(),
    }
    refs: dict[str, object] = {
        "campaign_id": campaign.id,
        "campaign_revision": campaign.revision,
        "sponsor_id": sponsor.id,
        "sponsor_revision": sponsor.revision,
        "commercial_controls_revision": control.revision,
        "contexts": context_snapshot,
        "booking_conflicts": conflict_snapshot,
    }
    input_facts = {
        "campaign_id": campaign.id,
        "campaign_revision": campaign.revision,
        "sponsor_id": sponsor.id,
        "sponsor_revision": sponsor.revision,
        "controls_revision": control.revision,
        "topic": request.topic,
        "starts_at": request.starts_at.isoformat(),
        "ends_at": request.ends_at.isoformat(),
        "contexts": context_snapshot,
        "conflicts": conflict_snapshot,
        "fee_minor": campaign.agreed_fee_minor,
    }
    input_hash = _canonical_hash(input_facts)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"africasignal:commercial:package:{input_hash}"},
    )
    existing = session.scalar(
        select(CommercialDraft).where(
            CommercialDraft.kind == "package", CommercialDraft.input_hash == input_hash
        )
    )
    if existing is not None:
        return _view(existing)
    row = CommercialDraft(
        campaign_id=campaign.id,
        kind="package",
        status="draft",
        revision=1,
        input_hash=input_hash,
        input_refs=refs,
        facts=facts,
        reason_codes=reason_codes,
        created_by_operator_id=operator.id,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        operator,
        "commercial.package_draft.create",
        "commercial_draft",
        row.id,
        after={"kind": row.kind, "input_hash": row.input_hash, "revision": row.revision},
    )
    return _view(row)


def list_package_drafts(session: Session, *, limit: int = 100) -> tuple[PackageDraftView, ...]:
    if not 1 <= limit <= 200:
        raise PackageError("package draft limit is outside allowed bounds")
    rows = session.scalars(
        select(CommercialDraft)
        .where(CommercialDraft.kind == "package")
        .order_by(CommercialDraft.created_at.desc(), CommercialDraft.id.desc())
        .limit(limit)
    ).all()
    return tuple(_view(row) for row in rows)


def review_package_draft(
    session: Session,
    operator: Operator,
    draft_id: int,
    *,
    status: Literal["approved", "rejected"],
    expected_revision: int,
    at: datetime | None = None,
) -> PackageDraftView:
    """Approve a still-current proposal snapshot; approval never books inventory."""
    _admin(operator)
    moment = at or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("review time must be timezone-aware")
    moment = moment.astimezone(UTC)
    row = session.scalar(
        select(CommercialDraft).where(CommercialDraft.id == draft_id).with_for_update()
    )
    if row is None or row.kind != "package":
        raise PackageError("package draft not found")
    if row.revision != expected_revision:
        raise PackageError("package revision conflict; reload before reviewing")
    if row.status != "draft":
        raise PackageError("package draft is already decided")

    if status == "approved":
        refs = row.input_refs
        campaign = session.scalar(
            select(Campaign).where(Campaign.id == row.campaign_id).with_for_update()
        )
        sponsor = session.get(Sponsor, campaign.sponsor_id) if campaign else None
        if (
            campaign is None
            or sponsor is None
            or campaign.revision != refs.get("campaign_revision")
            or sponsor.revision != refs.get("sponsor_revision")
            or campaign.status not in ("approved", "active")
            or sponsor.status != "approved"
        ):
            raise PackageError("package campaign inputs changed; regenerate the draft")
        controls = session.get(CommercialControl, 1)
        if controls is None or controls.revision != refs.get("commercial_controls_revision"):
            raise PackageError("commercial controls changed; regenerate the draft")
        if row.facts.get("price_status") != "known":
            raise PackageError("package price is unknown; agree a manual fee first")
        if row.facts.get("inventory_status") != "available_at_draft_time":
            raise PackageError("package inventory was unavailable or unknown; regenerate later")
        topic_value = row.facts.get("topic")
        if topic_value not in ("energy", "food"):
            raise PackageError("package topic is invalid; regenerate the draft")
        topic = cast(Topic, topic_value)
        starts_at = datetime.fromisoformat(str(row.facts["starts_at"]))
        ends_at = datetime.fromisoformat(str(row.facts["ends_at"]))
        current_conflicts = _conflicts(
            session,
            topic=topic,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        if current_conflicts != refs.get("booking_conflicts"):
            raise PackageError("package inventory changed; regenerate the draft")
        contexts = _eligible_contexts(
            session,
            topic=topic,
            at=moment,
        )[:100]
        if contexts != refs.get("contexts"):
            raise PackageError("eligible content changed; regenerate the draft")

    before = {"status": row.status, "revision": row.revision}
    row.status = status
    row.revision += 1
    row.reviewed_by_operator_id = operator.id
    row.reviewed_at = moment
    session.flush()
    audit.record(
        session,
        operator,
        "commercial.package_draft.review",
        "commercial_draft",
        row.id,
        before=before,
        after={"status": row.status, "revision": row.revision},
    )
    return _view(row)
