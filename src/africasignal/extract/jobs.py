"""Queueing claim extraction. Kept apart from the ``extract_claims`` handler so source adapters can
import it without pulling in the handler (which imports the NERC adapter)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from africasignal.extract.claims import extractor_version
from africasignal.jobs.queue import enqueue


def enqueue_extraction(session: Session, document_id: int) -> int | None:
    """Queue extraction of a document. The same document under the same prompt and model is queued
    at most once, so nothing is extracted twice."""
    return enqueue(
        session,
        "extract_claims",
        {"document_id": document_id},
        dedupe_key=f"extract_claims:{document_id}:{extractor_version()}",
    )
