"""Deterministic, content-only commercial context classification and persistence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import (
    AssessmentVersion,
    ContentContext,
    EvidenceDocument,
    Place,
    Situation,
    Source,
)
from africasignal.operations.commercial_context_contract import (
    ContextDecision,
    ContextInput,
    ContextRef,
)
from africasignal.publish.suspension import publication_suspended
from africasignal.sources.permissions import current_permission
from africasignal.web.queries import evidence_ids

TAXONOMY_VERSION = "commercial_taxonomy_v1"
CLASSIFIER_VERSION = "deterministic_rules_v1"
ELIGIBLE_TTL = timedelta(hours=6)
UNKNOWN_TTL = timedelta(minutes=15)


class ContextError(ValueError):
    """Safe error for stale or invalid commercial context references."""


class ContentContextView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    revision: int
    assessment_version_id: int
    canonical_place_id: int
    content_hash: str
    taxonomy_version: str
    classifier_version: str
    topic_tags: tuple[str, ...]
    evidence_refs: tuple[int, ...]
    source_refs: tuple[int, ...]
    reason_codes: tuple[str, ...]
    suitability: str
    expires_at: datetime
    invalidated_at: datetime | None
    invalidation_reason: str | None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _content_hash(situation: Situation, version: AssessmentVersion) -> str:
    payload = {
        "situation_id": situation.id,
        "situation_status": situation.status,
        "topic": situation.topic,
        "item_code": situation.item_code,
        "place_id": situation.place_id,
        "version_id": version.id,
        "version": version.version,
        "template": version.template,
        "template_version": version.template_version,
        "policy_version": version.policy_version,
        "inputs_hash": version.inputs_hash,
        "status": version.status,
        "evidence_state": version.evidence_state,
        "severity": version.severity,
        "headline": version.headline,
        "facts": version.facts,
        "explanation": version.explanation,
        "possible_factors": version.possible_factors,
        "unknowns": version.unknowns,
        "scope_label": version.scope_label,
        "period_label": version.period_label,
        "published_at": _iso(version.published_at),
        "valid_until": _iso(version.valid_until),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_ref_for(session: Session, assessment_version_id: int) -> ContextRef | None:
    row = session.execute(
        select(Situation, AssessmentVersion)
        .join(AssessmentVersion, AssessmentVersion.situation_id == Situation.id)
        .where(AssessmentVersion.id == assessment_version_id)
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        return None
    situation, version = row
    return ContextRef(
        assessment_version_id=version.id,
        content_hash=_content_hash(situation, version),
    )


def load_context_input(session: Session, *, content_ref: ContextRef) -> ContextInput | None:
    row = session.execute(
        select(Situation, AssessmentVersion, Place)
        .join(AssessmentVersion, AssessmentVersion.situation_id == Situation.id)
        .join(Place, Place.id == Situation.place_id)
        .where(AssessmentVersion.id == content_ref.assessment_version_id)
    ).first()
    if row is None:
        return None
    situation, version, place = row
    actual_hash = _content_hash(situation, version)
    if actual_hash != content_ref.content_hash:
        return None
    refs = tuple(evidence_ids(version))
    return ContextInput(
        content_ref=content_ref,
        topic=situation.topic,
        canonical_place_id=place.id,
        current=situation.current_version_id == version.id,
        situation_status=situation.status,
        status=version.status,
        evidence_state=version.evidence_state,
        published_at=version.published_at,
        valid_until=version.valid_until,
        evidence_refs=refs,
    )


def _source_reason(session: Session, refs: tuple[int, ...], now: datetime) -> str | None:
    if not refs:
        return "missing_evidence"
    for evidence_id in refs:
        document = session.get(EvidenceDocument, evidence_id)
        if (
            document is None
            or document.status != "active"
            or (document.retention_until is not None and document.retention_until <= now)
        ):
            return "withdrawn_evidence"
        source = session.get(Source, document.source_id)
        if source is None or not source.active:
            return "permission_denied"
        permission = current_permission(session, source.id)
        if (
            permission is None
            or permission.review_due_at is not None
            and permission.review_due_at <= now
        ):
            return "permission_unavailable"
        if not permission.may_collect or not permission.may_republish_numbers:
            return "permission_denied"
    return None


def _decision(
    session: Session,
    context: ContextInput,
    *,
    now: datetime,
) -> ContextDecision:
    reasons: list[str] = []
    suitability = "unknown"
    suspended = publication_suspended(session)
    if suspended:
        reasons.append("publication_suspended")
    elif context.situation_status == "closed":
        reasons.append("closed_situation")
    elif not context.current:
        reasons.append("not_current")
    elif context.status == "withdrawn":
        reasons.append("withdrawn_content")
    elif context.status != "published":
        reasons.append("not_published")
    elif context.valid_until is not None and context.valid_until <= now:
        reasons.append("expired")
    elif context.topic not in ("energy", "food"):
        reasons.append("unsupported_topic")
        suitability = "restricted"
    elif context.evidence_state == "disputed":
        reasons.append("disputed_evidence")
    elif context.evidence_state == "insufficient":
        reasons.append("insufficient_evidence")
    elif context.evidence_state not in ("reported", "corroborated"):
        reasons.append("insufficient_evidence")
    else:
        source_reason = _source_reason(session, context.evidence_refs, now)
        if source_reason is not None:
            reasons.append(source_reason)
        else:
            suitability = "eligible"
            reasons.append("current_public")
    expires_at = now + (ELIGIBLE_TTL if suitability == "eligible" else UNKNOWN_TTL)
    if context.valid_until is not None:
        expires_at = min(expires_at, context.valid_until)
    return ContextDecision(
        topic_tags=(context.topic,),
        canonical_place_id=context.canonical_place_id,
        taxonomy_version=TAXONOMY_VERSION,
        classifier_version=CLASSIFIER_VERSION,
        suitability=suitability,  # type: ignore[arg-type]
        reason_codes=tuple(reasons),  # type: ignore[arg-type]
        evidence_refs=context.evidence_refs,
        expires_at=expires_at,
    )


def classify_content(
    session: Session,
    *,
    content_ref: ContextRef,
    now: datetime | None = None,
) -> ContextDecision:
    """Apply fixed topic and publication/rights gates. Reads content only, never reader signals."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("classification time must be timezone-aware")
    context = load_context_input(session, content_ref=content_ref)
    if context is None:
        raise ContextError("content reference is missing or its hash is stale")
    return _decision(session, context, now=moment.astimezone(UTC))


def _signature(decision: ContextDecision) -> tuple[object, ...]:
    return (
        decision.topic_tags,
        decision.canonical_place_id,
        decision.taxonomy_version,
        decision.classifier_version,
        decision.suitability,
        decision.reason_codes,
        decision.evidence_refs,
    )


def _view(row: ContentContext) -> ContentContextView:
    return ContentContextView(
        id=row.id,
        revision=row.revision,
        assessment_version_id=row.assessment_version_id,
        canonical_place_id=row.canonical_place_id,
        content_hash=row.content_hash,
        taxonomy_version=row.taxonomy_version,
        classifier_version=row.classifier_version,
        topic_tags=tuple(row.topic_tags),
        evidence_refs=tuple(row.evidence_refs),
        source_refs=tuple(row.source_refs),
        reason_codes=tuple(row.reason_codes),
        suitability=row.suitability,
        expires_at=row.expires_at,
        invalidated_at=row.invalidated_at,
        invalidation_reason=row.invalidation_reason,
    )


def store_context_decision(
    session: Session,
    *,
    input_ref: ContextRef,
    decision: ContextDecision,
    now: datetime | None = None,
) -> ContentContextView:
    """Persist a deterministic result only if its exact current inputs still match."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("context storage time must be timezone-aware")
    moment = moment.astimezone(UTC)
    context = load_context_input(session, content_ref=input_ref)
    if context is None:
        raise ContextError("content reference is missing or its hash is stale")
    expected = _decision(session, context, now=moment)
    if _signature(decision) != _signature(expected):
        raise ContextError("context decision does not match current deterministic inputs")
    expiry = decision.expires_at.astimezone(UTC)
    if expiry <= moment or expiry > moment + ELIGIBLE_TTL:
        raise ContextError("context expiry is outside the allowed freshness window")
    if context.valid_until is not None and expiry > context.valid_until:
        raise ContextError("context cannot outlive published content")

    source_refs = sorted(
        set(
            session.scalars(
                select(EvidenceDocument.source_id)
                .where(EvidenceDocument.id.in_(decision.evidence_refs))
                .distinct()
            )
        )
    )
    latest = session.scalar(
        select(ContentContext)
        .where(
            ContentContext.assessment_version_id == input_ref.assessment_version_id,
            ContentContext.content_hash == input_ref.content_hash,
            ContentContext.taxonomy_version == decision.taxonomy_version,
            ContentContext.classifier_version == decision.classifier_version,
        )
        .order_by(ContentContext.revision.desc())
        .limit(1)
        .with_for_update()
    )
    if (
        latest is not None
        and latest.invalidated_at is None
        and latest.expires_at > moment
        and tuple(latest.topic_tags) == decision.topic_tags
        and latest.canonical_place_id == decision.canonical_place_id
        and tuple(latest.evidence_refs) == decision.evidence_refs
        and tuple(latest.source_refs) == tuple(source_refs)
        and tuple(latest.reason_codes) == decision.reason_codes
        and latest.suitability == decision.suitability
    ):
        return _view(latest)

    row = ContentContext(
        assessment_version_id=input_ref.assessment_version_id,
        canonical_place_id=decision.canonical_place_id,
        content_hash=input_ref.content_hash,
        taxonomy_version=decision.taxonomy_version,
        classifier_version=decision.classifier_version,
        revision=(latest.revision + 1) if latest is not None else 1,
        topic_tags=list(decision.topic_tags),
        evidence_refs=list(decision.evidence_refs),
        source_refs=source_refs,
        reason_codes=list(decision.reason_codes),
        suitability=decision.suitability,
        expires_at=expiry,
    )
    session.add(row)
    session.flush()
    return _view(row)
