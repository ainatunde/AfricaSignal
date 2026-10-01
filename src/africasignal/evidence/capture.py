"""Fetch a URL, store the raw bytes, and record an ``EvidenceDocument`` (spec B6.2, AS-005)."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.evidence.simhash import simhash
from africasignal.evidence.text import (
    HTML_MIMES,
    ExtractedText,
    detect_mime,
    extension_for,
    extract_text,
)
from africasignal.evidence.urls import canonicalise
from africasignal.models import EvidenceDocument, Source, SourcePermission
from africasignal.net.fetch import FetchResult, fetch_document
from africasignal.sources.permissions import current_permission
from africasignal.storage import ObjectStore, evidence_key

log = logging.getLogger("africasignal.evidence.capture")

# Excerpt length when a source has no quotation limit (``max_quote_chars`` is null).
DEFAULT_EXCERPT_CHARS = 500
MAX_BYLINE_CHARS = 200  # a feed's author field is untrusted text

Fetcher = Callable[..., FetchResult]

_PROCESSING_CONTENT_ATTRIBUTE = "_africasignal_processing_content"


def consume_processing_content(store: ObjectStore, document: EvidenceDocument) -> bytes:
    """Take freshly captured bytes for this job, or read the retained object on a retry.

    Non-retained source material is attached only to the in-memory ORM instance so an adapter can
    parse it in the same job that fetched it. It is never serialized to the database or object
    store.
    """
    content = getattr(document, _PROCESSING_CONTENT_ATTRIBUTE, None)
    if isinstance(content, bytes):
        delattr(document, _PROCESSING_CONTENT_ATTRIBUTE)
        return content
    return store.get(document.storage_key)


def clear_processing_content(document: EvidenceDocument) -> None:
    """Drop any transient source bytes when document processing finishes."""
    if hasattr(document, _PROCESSING_CONTENT_ATTRIBUTE):
        delattr(document, _PROCESSING_CONTENT_ATTRIBUTE)


class CaptureError(Exception):
    """A document could not be captured."""


class SourceNotApproved(CaptureError):
    """The source has no approved permission, or its permission forbids collecting."""


def excerpt(text: str | None, permission: SourcePermission) -> str | None:
    """The part of ``text`` the source's permission lets readers see as a quotation."""
    if not text:
        return None
    limit = permission.max_quote_chars
    return text[: DEFAULT_EXCERPT_CHARS if limit is None else limit]


def capture(
    session: Session,
    store: ObjectStore,
    source: Source,
    url: str,
    *,
    fetch: Fetcher = fetch_document,
    published_at: datetime | None = None,
    title: str | None = None,
    byline: str | None = None,
    now: datetime | None = None,
) -> EvidenceDocument:
    """Fetch ``url`` for ``source`` and return its evidence record.

    Identical content at the same canonical URL returns the existing row and stores nothing new;
    changed content creates a new row. Full text is kept only when the source's approved
    permission allows it. The caller commits.
    """
    permission = current_permission(session, source.id)
    if permission is None or not permission.may_collect:
        raise SourceNotApproved(f"source {source.slug!r} has no approved permission to collect")

    result = fetch(url, max_requests_per_hour=source.max_requests_per_hour)
    if not result.success:
        raise CaptureError(result.error or f"fetch of {url} returned HTTP {result.status_code}")

    return record_document(
        session,
        store,
        source,
        permission,
        url=url,
        final_url=result.url,
        content=result.content,
        content_type=result.headers.get("content-type"),
        published_at=published_at,
        title=title,
        byline=byline,
        now=now,
    )


def record_document(
    session: Session,
    store: ObjectStore,
    source: Source,
    permission: SourcePermission,
    *,
    url: str,
    content: bytes,
    content_type: str | None = None,
    final_url: str | None = None,
    published_at: datetime | None = None,
    title: str | None = None,
    byline: str | None = None,
    now: datetime | None = None,
) -> EvidenceDocument:
    """Store ``content`` and record it as evidence for ``source``.

    Shared by ``capture`` (bytes just fetched) and by operator uploads (bytes the operator
    supplies with the URL they came from). Identical content at the same canonical URL returns the
    existing row; changed content creates a new one. ``published_at`` and ``title`` fill in what
    the file itself does not say (a spreadsheet has neither). The caller commits.
    """
    sha = hashlib.sha256(content).hexdigest()
    mime = detect_mime(content_type, content, final_url or url)
    extracted: ExtractedText = extract_text(content, mime)

    html = content.decode("utf-8", errors="replace") if mime in HTML_MIMES else None
    canonical = canonicalise(final_url or url, html)

    existing = session.scalars(
        select(EvidenceDocument).where(
            EvidenceDocument.source_id == source.id,
            EvidenceDocument.canonical_url == canonical,
            EvidenceDocument.content_sha256 == sha,
        )
    ).first()
    if existing is not None:
        setattr(existing, _PROCESSING_CONTENT_ATTRIBUTE, content)
        return existing

    quotation = excerpt(extracted.text, permission)
    if permission.may_store_full_text:
        key = evidence_key(sha, extension_for(mime))
        retained, retained_mime = content, mime
    else:
        # A raw HTML/PDF is itself a stored full copy. Retain only the permitted quotation.
        retained = (quotation or "").encode()
        key = evidence_key(hashlib.sha256(retained).hexdigest(), "quote")
        retained_mime = "text/plain"
    if not store.exists(key):
        store.put(key, retained, retained_mime)

    retrieved_at = now or datetime.now(UTC)
    document = EvidenceDocument(
        source_id=source.id,
        url=url,
        canonical_url=canonical,
        retrieved_at=retrieved_at,
        published_at=published_at or extracted.published_at,
        content_sha256=sha,
        storage_key=key,
        mime=mime,
        title=title or extracted.title,
        byline=(byline or "").strip()[:MAX_BYLINE_CHARS] or None,
        text_content=extracted.text if permission.may_store_full_text else None,
        excerpt=quotation,
        simhash=simhash(extracted.text) if extracted.text else None,
        retention_until=(
            retrieved_at + timedelta(days=permission.retention_days)
            if permission.retention_days is not None
            else None
        ),
    )
    session.add(document)
    session.flush()
    setattr(document, _PROCESSING_CONTENT_ATTRIBUTE, content)
    return document


def load_text(store: ObjectStore, document: EvidenceDocument) -> str | None:
    """Return retained text only; never reconstruct a forbidden full copy from raw bytes."""
    if document.status in ("expired", "withdrawn"):
        return None
    return document.text_content if document.text_content is not None else document.excerpt
