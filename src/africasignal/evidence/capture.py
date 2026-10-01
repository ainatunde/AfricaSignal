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

Fetcher = Callable[..., FetchResult]


class CaptureError(Exception):
    """A document could not be captured."""


class SourceNotApproved(CaptureError):
    """The source has no approved permission, or its permission forbids collecting."""


def _excerpt(text: str | None, permission: SourcePermission) -> str | None:
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
        return existing

    key = evidence_key(sha, extension_for(mime))
    if not store.exists(key):
        store.put(key, content, mime)

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
        text_content=extracted.text if permission.may_store_full_text else None,
        excerpt=_excerpt(extracted.text, permission),
        simhash=simhash(extracted.text) if extracted.text else None,
        retention_until=(
            retrieved_at + timedelta(days=permission.retention_days)
            if permission.retention_days is not None
            else None
        ),
    )
    session.add(document)
    session.flush()
    return document


def load_text(store: ObjectStore, document: EvidenceDocument) -> str | None:
    """The document's text: the stored copy, or re-extracted from the raw object when the
    source's permission does not allow storing full text."""
    if document.text_content is not None:
        return document.text_content
    return extract_text(store.get(document.storage_key), document.mime).text
