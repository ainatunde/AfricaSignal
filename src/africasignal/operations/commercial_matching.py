"""Explainable sponsor matching from curated business categories and current content context."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import ContentContext, Sponsor
from africasignal.operations.commercial_context import (
    CLASSIFIER_VERSION,
    TAXONOMY_VERSION,
    ContextError,
    classify_content,
)
from africasignal.operations.commercial_context_contract import ContextRef

Topic = Literal["energy", "food"]
CATEGORY_TOPICS: dict[str, frozenset[str]] = {
    "energy_provider": frozenset({"energy"}),
    "energy_efficiency": frozenset({"energy"}),
    "food_retailer": frozenset({"food"}),
    "agriculture": frozenset({"food"}),
    "general_business": frozenset(),
    "unclassified": frozenset(),
}


class SponsorMatchProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    sponsor_id: int
    public_name: str
    category: str
    matched: bool
    reason_codes: tuple[str, ...]


class MatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    content_ref: ContextRef
    topic: Topic | None
    context_eligible: bool
    reason_codes: tuple[str, ...]
    context_id: int | None
    context_revision: int | None
    proposals: tuple[SponsorMatchProposal, ...]


def match_sponsors(
    session: Session, *, content_ref: ContextRef, at: datetime | None = None
) -> MatchResult:
    """Match approved sponsors to one current eligible content context; never infer fit scores."""
    moment = at or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("matching time must be timezone-aware")
    moment = moment.astimezone(UTC)
    projection = session.scalar(
        select(ContentContext)
        .where(
            ContentContext.assessment_version_id == content_ref.assessment_version_id,
            ContentContext.content_hash == content_ref.content_hash,
            ContentContext.taxonomy_version == TAXONOMY_VERSION,
            ContentContext.classifier_version == CLASSIFIER_VERSION,
            ContentContext.invalidated_at.is_(None),
            ContentContext.expires_at > moment,
        )
        .order_by(ContentContext.revision.desc())
        .limit(1)
    )
    if projection is None:
        return MatchResult(
            content_ref=content_ref,
            topic=None,
            context_eligible=False,
            reason_codes=("context_projection_unavailable",),
            context_id=None,
            context_revision=None,
            proposals=(),
        )
    try:
        decision = classify_content(session, content_ref=content_ref, now=moment)
    except ContextError:
        return MatchResult(
            content_ref=content_ref,
            topic=None,
            context_eligible=False,
            reason_codes=("stale_content_reference",),
            context_id=projection.id,
            context_revision=projection.revision,
            proposals=(),
        )
    topic = decision.topic_tags[0] if decision.topic_tags else None
    if decision.suitability != "eligible" or topic is None:
        return MatchResult(
            content_ref=content_ref,
            topic=topic,
            context_eligible=False,
            reason_codes=decision.reason_codes,
            context_id=projection.id,
            context_revision=projection.revision,
            proposals=(),
        )

    sponsors = session.scalars(
        select(Sponsor).where(Sponsor.status == "approved").order_by(Sponsor.id).limit(200)
    ).all()
    proposals = tuple(
        SponsorMatchProposal(
            sponsor_id=sponsor.id,
            public_name=sponsor.public_name,
            category=sponsor.category,
            matched=topic in CATEGORY_TOPICS.get(sponsor.category, frozenset()),
            reason_codes=(
                (
                    "approved_sponsor",
                    "current_eligible_context",
                    f"curated_category_{sponsor.category}",
                    "operator_review_required",
                )
                if topic in CATEGORY_TOPICS.get(sponsor.category, frozenset())
                else ("approved_sponsor", "curated_category_mismatch")
            ),
        )
        for sponsor in sponsors
    )
    return MatchResult(
        content_ref=content_ref,
        topic=topic,
        context_eligible=True,
        reason_codes=("current_public",),
        context_id=projection.id,
        context_revision=projection.revision,
        proposals=proposals,
    )
