"""Permission-aware retrieval of the exact claims already used by an assessment.

This is a first RAG context layer, not a competing article store. It deliberately retrieves only
validated passages linked to the assessment, after rechecking current numeric reuse rights,
quotation bounds, and evidence retention. A collection pause does not erase reuse rights for
retained material. Semantic/vector expansion stays out until retrieval quality is measured on
representative Nigeria-focused cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal.models import (
    AssessmentInput,
    Claim,
    EvidenceDocument,
    Source,
    SourcePermission,
)

MAX_PASSAGES = 8
MAX_PASSAGE_CHARS = 500


@dataclass(frozen=True)
class RetrievedPassage:
    """An exact, permitted claim excerpt and its source attribution for a synthesis prompt."""

    claim_id: int
    source: str
    published: str
    passage: str

    def prompt_value(self) -> dict[str, str]:
        # The database ID is retained for validation, but is not shown to the model.
        return {"source": self.source, "published": self.published, "passage": self.passage}


def assessment_passages(
    session: Session, assessment_version_id: int, *, now: datetime | None = None
) -> list[RetrievedPassage]:
    """Return only current, valid source passages selected by this assessment version."""
    now = now or datetime.now(UTC)
    latest_permission = (
        select(
            SourcePermission.source_id.label("source_id"),
            func.max(SourcePermission.version).label("version"),
        )
        .where(SourcePermission.approved_at.is_not(None))
        .group_by(SourcePermission.source_id)
        .subquery()
    )
    rows = session.execute(
        select(Claim, EvidenceDocument, Source, SourcePermission)
        .join(
            AssessmentInput,
            (AssessmentInput.input_id == Claim.id) & (AssessmentInput.input_kind == "claim"),
        )
        .join(EvidenceDocument, EvidenceDocument.id == Claim.evidence_document_id)
        .join(Source, Source.id == EvidenceDocument.source_id)
        .join(latest_permission, latest_permission.c.source_id == Source.id)
        .join(
            SourcePermission,
            (SourcePermission.source_id == latest_permission.c.source_id)
            & (SourcePermission.version == latest_permission.c.version),
        )
        .where(
            AssessmentInput.assessment_version_id == assessment_version_id,
            Claim.valid.is_(True),
            EvidenceDocument.status == "active",
            (EvidenceDocument.retention_until.is_(None) | (EvidenceDocument.retention_until > now)),
            SourcePermission.may_republish_numbers.is_(True),
        )
        .order_by(EvidenceDocument.published_at.desc().nullslast(), Claim.id.desc())
        .limit(MAX_PASSAGES * 3)
    )
    result: list[RetrievedPassage] = []
    seen_documents: set[int] = set()
    for claim, document, source, permission in rows:
        # Collection may be paused while stored evidence remains reusable. The active retention
        # window is the stricter of the document's captured limit and the current permission.
        if permission.retention_days is not None and (
            document.retrieved_at + timedelta(days=permission.retention_days) <= now
        ):
            continue
        if document.id in seen_documents:
            continue
        passage = claim.passage.strip()
        if not passage:
            continue
        if permission.max_quote_chars is not None and len(passage) > permission.max_quote_chars:
            continue
        if not permission.may_store_full_text and (
            not document.excerpt or passage not in document.excerpt
        ):
            continue
        passage = passage[:MAX_PASSAGE_CHARS]
        result.append(
            RetrievedPassage(
                claim_id=claim.id,
                source=source.name[:160],
                published=(document.published_at or document.retrieved_at).date().isoformat(),
                passage=passage,
            )
        )
        seen_documents.add(document.id)
        if len(result) >= MAX_PASSAGES:
            break
    return result


def assessment_evidence_is_current(
    session: Session, assessment_version_id: int, *, now: datetime | None = None
) -> bool:
    """Backwards-compatible wrapper for the shared, channel-aware publication decision."""
    from africasignal.models import AssessmentVersion
    from africasignal.publish.eligibility import publication_eligibility

    version = session.get(AssessmentVersion, assessment_version_id)
    if version is None:
        return False
    return publication_eligibility(
        session,
        version,
        channel="email",
        now=now or datetime.now(UTC),
    ).eligible
