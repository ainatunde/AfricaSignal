"""Reporting-origin clustering (spec B3.2, AS-025).

Two outlets that print the same wire story are one source of information, not two. Every
evidence document is assigned a ``ReportingOrigin``; corroboration counts origins, not documents.

Rules, in order:

1. The same canonical URL is the same origin (a page that was edited and captured again).
2. Official sources (statistics, regulators, government, companies) never merge by text: each
   document is its own origin, ``official_dataset`` or ``primary_document``.
3. Otherwise a document whose SimHash is within ``SIMHASH_MAX_DISTANCE`` bits of another
   non-official document published within ``WINDOW_DAYS`` of it is a syndicated copy: same origin.
4. Anything else starts a new origin.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import BigInteger, cast, func, literal, select
from sqlalchemy.dialects.postgresql import BIT
from sqlalchemy.orm import Session

from africasignal.models import Claim, EvidenceDocument, ReportingOrigin, Source

log = logging.getLogger("africasignal.evidence.origins")

SIMHASH_MAX_DISTANCE = 3
WINDOW_DAYS = 14

_OFFICIAL_KIND = {
    "official_statistics": "official_dataset",
    "regulator": "primary_document",
    "government": "primary_document",
    "company": "primary_document",
}
# Origin kinds a near-duplicate may join. Official origins are never merged by text.
_CLUSTERABLE_KINDS = ("outlet_report", "wire_report", "unknown")


def _when(document: EvidenceDocument) -> datetime:
    return document.published_at or document.retrieved_at


def _label(source: Source, document: EvidenceDocument) -> str:
    if source.kind in _OFFICIAL_KIND:
        name = document.title or source.name
        return f"{name}, {_when(document):%-d %b %Y}"
    return f"{source.name} report, {_when(document):%-d %b %Y}"


def _new_origin_kind(source: Source) -> str:
    if source.kind in _OFFICIAL_KIND:
        return _OFFICIAL_KIND[source.kind]
    return "unknown" if source.kind == "aggregator" else "outlet_report"


def _same_url_origin(session: Session, document: EvidenceDocument) -> ReportingOrigin | None:
    return session.scalars(
        select(ReportingOrigin)
        .join(EvidenceDocument, EvidenceDocument.origin_id == ReportingOrigin.id)
        .where(
            EvidenceDocument.canonical_url == document.canonical_url,
            EvidenceDocument.id != document.id,
        )
        .order_by(EvidenceDocument.id)
        .limit(1)
    ).first()


def _near_duplicate(
    session: Session, document: EvidenceDocument
) -> tuple[ReportingOrigin, EvidenceDocument] | None:
    """The origin of the closest clusterable document within the time window, if close enough."""
    if document.simhash is None:
        return None
    moment = func.coalesce(EvidenceDocument.published_at, EvidenceDocument.retrieved_at)
    window = timedelta(days=WINDOW_DAYS)
    distance = func.bit_count(
        cast(
            EvidenceDocument.simhash.op("#")(cast(literal(document.simhash), BigInteger)),
            BIT(64),
        )
    )
    row = session.execute(
        select(ReportingOrigin, EvidenceDocument)
        .join(EvidenceDocument, EvidenceDocument.origin_id == ReportingOrigin.id)
        .where(
            EvidenceDocument.id != document.id,
            EvidenceDocument.simhash.is_not(None),
            EvidenceDocument.status == "active",
            ReportingOrigin.kind.in_(_CLUSTERABLE_KINDS),
            moment >= _when(document) - window,
            moment <= _when(document) + window,
            distance <= SIMHASH_MAX_DISTANCE,
        )
        .order_by(distance, ReportingOrigin.first_seen_at, EvidenceDocument.id)
        .limit(1)
    ).first()
    return None if row is None else (row[0], row[1])


def assign_origin(session: Session, document: EvidenceDocument) -> ReportingOrigin:
    """Give ``document`` its reporting origin (creating one if needed) and return it.

    Idempotent: a document that already has an origin keeps it. The caller commits.
    """
    if document.origin_id is not None:
        existing = session.get(ReportingOrigin, document.origin_id)
        if existing is not None:
            return existing

    source = session.get(Source, document.source_id)
    if source is None:
        raise ValueError(f"evidence document {document.id} has no source {document.source_id}")

    origin = _same_url_origin(session, document)
    if origin is None and source.kind not in _OFFICIAL_KIND:
        match = _near_duplicate(session, document)
        if match is not None:
            origin, copied_from = match
            if origin.kind == "outlet_report" and copied_from.source_id != source.id:
                origin.kind = "wire_report"  # the same text in two outlets is a wire story
            log.info(
                "document %s is a copy of document %s (origin %s)",
                document.id,
                copied_from.id,
                origin.id,
            )
    if origin is None:
        origin = ReportingOrigin(
            kind=_new_origin_kind(source),
            label=_label(source, document),
            first_seen_at=_when(document),
        )
        session.add(origin)
        session.flush()

    document.origin_id = origin.id
    session.flush()
    return origin


def origin_ids(session: Session, claims: Iterable[Claim]) -> set[int]:
    """The distinct reporting origins behind ``claims``.

    Claims on withdrawn or expired documents are ignored, and so are documents that have no origin
    yet: a document nobody has clustered cannot be shown to be independent.
    """
    document_ids = {claim.evidence_document_id for claim in claims}
    if not document_ids:
        return set()
    return set(
        session.scalars(
            select(EvidenceDocument.origin_id)
            .where(
                EvidenceDocument.id.in_(document_ids),
                EvidenceDocument.status == "active",
                EvidenceDocument.origin_id.is_not(None),
            )
            .distinct()
        )
    )


def independent_origin_count(session: Session, claims: Iterable[Claim]) -> int:
    """How many independent reporting origins the claims come from."""
    return len(origin_ids(session, claims))
