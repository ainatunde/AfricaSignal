"""Text extraction: trafilatura for HTML, pdfplumber for PDF."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import pdfplumber
import trafilatura

log = logging.getLogger("africasignal.evidence.text")

HTML_MIMES = ("text/html", "application/xhtml+xml")
_EXTENSIONS = {
    "text/html": "html",
    "application/xhtml+xml": "html",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "application/xml": "xml",
    "text/xml": "xml",
    "application/rss+xml": "xml",
    "application/atom+xml": "xml",
    "application/json": "json",
    "text/plain": "txt",
    "text/csv": "csv",
}
_URL_SUFFIX_MIMES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".xml": "application/xml",
    ".json": "application/json",
    ".csv": "text/csv",
}


@dataclass(frozen=True)
class ExtractedText:
    text: str | None
    title: str | None = None
    published_at: datetime | None = None


def detect_mime(content_type: str | None, content: bytes, url: str = "") -> str:
    """Media type from the header, falling back to magic bytes and then the URL suffix."""
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return declared
    if content.startswith(b"%PDF"):
        return "application/pdf"
    head = content[:512].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        return "text/html"
    path = url.split("?")[0].split("#")[0].lower()
    for suffix, mime in _URL_SUFFIX_MIMES.items():
        if path.endswith(suffix):
            return mime
    return declared or "application/octet-stream"


def extension_for(mime: str) -> str:
    return _EXTENSIONS.get(mime, "bin")


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        return None


def extract_text(content: bytes, mime: str) -> ExtractedText:
    """Readable text from a document, or ``ExtractedText(None)`` for formats without prose."""
    if mime in HTML_MIMES:
        html = content.decode("utf-8", errors="replace")
        text = trafilatura.extract(html)
        meta = trafilatura.extract_metadata(html)
        return ExtractedText(
            text=text or None,
            title=meta.title if meta else None,
            published_at=_parse_date(meta.date if meta else None),
        )
    if mime == "application/pdf":
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
        except Exception:
            log.warning("could not read PDF text", exc_info=True)
            return ExtractedText(None)
        text = "\n\n".join(p for p in pages if p.strip())
        return ExtractedText(text or None)
    return ExtractedText(None)
