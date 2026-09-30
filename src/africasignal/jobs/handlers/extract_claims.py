"""``extract_claims``: pull validated claims out of one captured document (spec B7, AS-021).

Payload: ``{"document_id": int}``. Enqueue it with ``enqueue_extraction``.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.evidence.capture import load_text
from africasignal.extract.claims import (
    PURPOSE,
    extract_claims,
    extractor_version,
    keyword_hits,
    store_claims,
)
from africasignal.jobs.handlers import JobContext, register
from africasignal.jobs.handlers.resolve_places import enqueue_place_resolution
from africasignal.jobs.queue import enqueue
from africasignal.llm import BudgetExhausted, build_adapter
from africasignal.llm.budget import defer_until_next_day
from africasignal.models import Claim, EvidenceDocument
from africasignal.storage import get_store

log = logging.getLogger("africasignal.extract_claims")


def enqueue_extraction(session: Session, document_id: int) -> int | None:
    """Queue extraction of a document. The same document under the same prompt and model is queued
    at most once, so nothing is extracted twice."""
    return enqueue(
        session,
        "extract_claims",
        {"document_id": document_id},
        dedupe_key=f"extract_claims:{document_id}:{extractor_version()}",
    )


@register("extract_claims")
def extract_claims_job(ctx: JobContext) -> None:
    session = ctx.session
    document_id = int(ctx.job.payload["document_id"])
    log_extra = {"job_id": ctx.job.id}
    document = session.get(EvidenceDocument, document_id)
    if document is None or document.status != "active":
        log.info("skipping document %s: missing or not active", document_id, extra=log_extra)
        return

    version = extractor_version()
    already = session.scalar(
        select(Claim.id)
        .where(Claim.evidence_document_id == document_id, Claim.extractor_version == version)
        .limit(1)
    )
    if already is not None:
        log.info("document %s already extracted as %s", document_id, version, extra=log_extra)
        return

    text = document.text_content
    if text is None:  # the source's permission does not allow storing text: read the raw file
        text = load_text(get_store(), document)
    if not text or not keyword_hits(text):
        log.info("document %s has no topic keywords: not extracted", document_id, extra=log_extra)
        return

    try:
        extraction = extract_claims(build_adapter(session), document, text, job_id=ctx.job.id)
    except BudgetExhausted as exc:
        defer_until_next_day(
            session,
            "extract_claims",
            ctx.job.payload,
            dedupe_key=f"extract_claims:{document_id}:{version}",
            retry_at=exc.retry_at,
        )
        log.info(
            "daily %s budget spent: document %s deferred to %s",
            PURPOSE,
            document_id,
            exc.retry_at.isoformat(),
            extra=log_extra,
        )
        return

    claims = store_claims(session, document, extraction, version)
    valid = [c for c in claims if c.valid]
    if any(c.place_candidates for c in valid):
        enqueue_place_resolution(session, document_id, version)
    log.info(
        "document %s: %d claims, %d valid",
        document_id,
        len(claims),
        len(valid),
        extra=log_extra,
    )
