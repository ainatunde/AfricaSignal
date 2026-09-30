"""NERC orders adapter (spec B6.4, AS-026).

Discovery reads the orders listing at ``nerc.gov.ng/resource-category/orders/`` (newest first,
page 1): each entry is a title, a date and a PDF. Processing reads the PDF:

* Plain orders have a text layer; pdfplumber reads it at capture and nothing more is done here.
  Their policy statements are left to claim extraction (AS-021).
* The monthly DisCo tariff schedules ("IE MYTO SEPTEMBER 2026") are drawn, not typed, so the PDF is
  read with OCR (``evidence.ocr``). The text is kept on the document (when the source's permission
  allows full text) so the language-model extraction and every reader of the evidence see the same
  words. The Band A tariff is then parsed from the OCR text *in code* (``tariff_parse``) and stored
  as ``policy_statement`` claims for the DisCo's series ``electricity_tariff_band_a:<slug>``, one
  per period column of the tariff table (the current tariff and the ones before it).

Claims are stored only for DisCos whose series is listed in ``config/policies.yaml``.

A language model's tariff claims are checked against the same code-parsed figures by
``reconcile_tariff_claims``: a stated value code cannot find in the document makes the claim invalid
(``tariff_value_mismatch``), so the model never decides a tariff.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import urljoin, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.catalog import load_policies
from africasignal.evidence.ocr import has_text_layer, ocr_pdf
from africasignal.evidence.simhash import simhash
from africasignal.evidence.text import extract_text
from africasignal.models import Claim, EvidenceDocument, ReportingOrigin, Source
from africasignal.net.fetch import FetchResult, fetch_document
from africasignal.sources.base import (
    AdapterContext,
    DiscoveredItem,
    ProcessResult,
    register_adapter,
)
from africasignal.sources.permissions import current_permission
from africasignal.sources.tariff_parse import (
    BandATable,
    TableRejected,
    band_a_statements,
    band_a_table,
    effective_date,
    tariff_values,
)

log = logging.getLogger("africasignal.sources.nerc")

EXTRACTOR_VERSION = "nerc_tariff_v1"
TARIFF_UNIT = "NGN/kWh"
LISTING_MAX_BYTES = 2_000_000  # page 1 was 163 KB when this was written
DEFAULT_LISTING_URL = "https://nerc.gov.ng/resource-category/orders/"
TARIFF_SERIES_PREFIX = "electricity_tariff_band_a:"
MISMATCH = "tariff_value_mismatch"

# NERC's abbreviation for each distribution company, as used in the titles and file names of its
# monthly orders ("IE MYTO SEPTEMBER 2026", "EKEDP_HOLDCO_YSS_..."), and the slug that ends the
# DisCo's policy series code. A DisCo whose series is not in ``config/policies.yaml`` is skipped.
DISCO_SLUGS = {
    "AEDC": "abuja-electricity",
    "BEDC": "benin-electricity",
    "EEDC": "enugu-electricity",
    "EKEDP": "eko",
    "IBEDC": "ibadan-electricity",
    "IE": "ikeja-electric",
    "JED": "jos-electricity",
    "KAEDC": "kaduna-electricity",
    "KEDCO": "kano-electricity",
    "PHED": "port-harcourt-electricity",
    "YEDC": "yola-electricity",
}
_TITLE_CODE = re.compile(r"^\s*(?P<code>[A-Z]{2,6})\s+MYTO\b", re.IGNORECASE)
_FILE_CODE = re.compile(r"/(?P<code>[A-Za-z]{2,6})_HOLDCO_", re.IGNORECASE)
_PUBLICATION = re.compile(
    r'<div class="publication">(?P<body>.*?)(?=<div class="publication">|$)', re.S
)
_TITLE = re.compile(r'<h6 class="title">(?P<title>.*?)</h6>', re.S)
_DATE = re.compile(r'<a[^>]*title="(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})"')
_PDF_LINK = re.compile(r'href="(?P<href>[^"#]+\.pdf)"', re.IGNORECASE)

Fetcher = Callable[..., FetchResult]


class NercError(Exception):
    """The NERC listing or an order could not be read."""


def parse_listing(page_html: str, base_url: str = "") -> list[DiscoveredItem]:
    """Every order on an orders listing page that has a PDF, newest first as listed."""
    items = []
    for block in _PUBLICATION.finditer(page_html):
        body = block["body"]
        title = _TITLE.search(body)
        link = _PDF_LINK.search(body)
        if title is None or link is None:
            continue
        stamp = _DATE.search(body)
        published = None
        if stamp is not None:
            try:
                day = datetime.strptime(stamp["date"], "%B %d, %Y")
                published = day.replace(tzinfo=UTC)
            except ValueError:
                published = None
        items.append(
            DiscoveredItem(
                url=urljoin(base_url, html.unescape(link["href"])),
                title=re.sub(r"\s+", " ", html.unescape(title["title"])).strip(),
                published_at=published,
            )
        )
    return items


def disco_code(title: str | None, url: str) -> str | None:
    """NERC's abbreviation of the DisCo a monthly order is for, or None for any other order."""
    for pattern, subject in ((_TITLE_CODE, title or ""), (_FILE_CODE, url)):
        m = pattern.search(subject)
        if m and m["code"].upper() in DISCO_SLUGS:
            return m["code"].upper()
    return None


def _same_site(url: str, home_url: str) -> bool:
    host, home = urlparse(url).hostname or "", urlparse(home_url).hostname or ""
    return host == home or host.endswith("." + home)


def _locate(text: str, passage: str) -> tuple[int, int] | None:
    start = text.find(passage)
    return (start, start + len(passage)) if start >= 0 else None


def _origin(session: Session, document: EvidenceDocument, label: str) -> None:
    if document.origin_id is not None:
        return
    origin = ReportingOrigin(
        kind="primary_document", label=label, first_seen_at=document.retrieved_at
    )
    session.add(origin)
    session.flush()
    document.origin_id = origin.id


def _direction(previous: Decimal | None, value: Decimal) -> str:
    if previous is None:
        return "unknown"
    return "up" if value > previous else "down" if value < previous else "unchanged"


class NercAdapter:
    slug_prefix = "nerc"

    def __init__(self, fetch: Fetcher = fetch_document) -> None:
        self._fetch = fetch

    def discover(self, source: Source, ctx: AdapterContext) -> list[DiscoveredItem]:
        url = source.feed_url or DEFAULT_LISTING_URL
        result = self._fetch(
            url, max_requests_per_hour=source.max_requests_per_hour, max_bytes=LISTING_MAX_BYTES
        )
        if not result.success:
            raise NercError(result.error or f"fetch of {url} returned HTTP {result.status_code}")
        listed = parse_listing(result.content.decode("utf-8", errors="replace"), result.url)
        if not listed:
            raise NercError("the orders listing has no entries; the page layout may have changed")
        known = set(
            ctx.session.scalars(
                select(EvidenceDocument.url).where(EvidenceDocument.source_id == source.id)
            )
        )
        home = source.home_url or url
        items = []
        for item in listed:
            if item.url in known:
                continue
            if not _same_site(item.url, home):
                log.warning("nerc: %r links to another site (%s); skipped", item.title, item.url)
                continue
            items.append(item)
        return items

    def process(self, doc: EvidenceDocument, ctx: AdapterContext) -> ProcessResult:
        result = ProcessResult()
        if doc.mime != "application/pdf":
            result.notes.append(f"not a PDF ({doc.mime}); nothing read")
            return result
        source = ctx.session.get(Source, doc.source_id)
        if source is None:
            raise NercError(f"document {doc.id} has no source")
        _origin(ctx.session, doc, source.name)

        content = ctx.store.get(doc.storage_key)
        doubtful: frozenset[str] = frozenset()
        typed = extract_text(content, doc.mime).text if has_text_layer(content) else None
        if typed is not None:
            text, method = typed, "text layer"
        else:
            ocr = ocr_pdf(content)  # raises OcrUnavailable when tesseract is missing
            text, doubtful, method = ocr.text, ocr.doubtful_lines, f"OCR ({ocr.pages} pages)"
            self._keep_text(ctx.session, doc, text)
        result.notes.append(f"read by {method}")

        code = disco_code(doc.title, doc.url)
        if code is None:
            result.notes.append("not a DisCo tariff order: no tariff claims made by code")
            return result
        series = TARIFF_SERIES_PREFIX + DISCO_SLUGS[code]
        if series not in {s.code for s in load_policies().series}:
            result.notes.append(f"{code}: no policy series {series} in policies.yaml; skipped")
            return result
        already = ctx.session.scalar(
            select(Claim.id)
            .where(
                Claim.evidence_document_id == doc.id, Claim.extractor_version == EXTRACTOR_VERSION
            )
            .limit(1)
        )
        if already is not None:
            result.notes.append("tariff claims already stored for this document")
            return result

        claims = self._tariff_claims(doc, series, text, doubtful, result.notes)
        ctx.session.add_all(claims)
        ctx.session.flush()
        result.claims = len(claims)
        return result

    @staticmethod
    def _keep_text(session: Session, doc: EvidenceDocument, text: str) -> None:
        """Keep the OCR text on the document when the permission allows full text."""
        permission = current_permission(session, doc.source_id)
        if permission is not None and permission.may_store_full_text:
            doc.text_content = text
            doc.simhash = simhash(text)

    def _tariff_claims(
        self,
        doc: EvidenceDocument,
        series: str,
        text: str,
        doubtful: frozenset[str],
        notes: list[str],
    ) -> list[Claim]:
        table = band_a_table(text, doubtful)
        if isinstance(table, TableRejected):
            notes.append(f"Band A table not used: {table.reason}")
        elif isinstance(table, BandATable):
            return self._table_claims(doc, series, text, table)
        statements = band_a_statements(text)
        if not statements:
            notes.append("no Band A tariff found in the text")
            return []
        effective = effective_date(text)
        claims = []
        for s in statements:
            span = _locate(text, s.passage)
            claims.append(
                self._claim(
                    doc,
                    series,
                    text=f"Band A tariff of ₦{s.value}/kWh",
                    passage=s.passage,
                    span=span,
                    value=s.value,
                    direction="unknown",
                    occurred_from=effective,
                    occurred_to=None,
                    precision="day" if effective else "unknown",
                )
            )
        return claims

    def _table_claims(
        self, doc: EvidenceDocument, series: str, text: str, table: BandATable
    ) -> list[Claim]:
        span = _locate(text, table.passage)
        claims = []
        previous: Decimal | None = None
        for col in table.columns:
            claims.append(
                self._claim(
                    doc,
                    series,
                    text=(
                        f"Band A tariff of ₦{col.value}/kWh, "
                        f"{col.period_start:%B %Y} to {col.period_end:%B %Y}"
                    ),
                    passage=table.passage,
                    span=span,
                    value=col.value,
                    direction=_direction(previous, col.value),
                    occurred_from=col.period_start,
                    occurred_to=col.period_end,
                    precision="month",
                )
            )
            previous = col.value
        return claims

    @staticmethod
    def _claim(
        doc: EvidenceDocument,
        series: str,
        *,
        text: str,
        passage: str,
        span: tuple[int, int] | None,
        value: Decimal,
        direction: str,
        occurred_from: date | None,
        occurred_to: date | None,
        precision: str,
    ) -> Claim:
        return Claim(
            evidence_document_id=doc.id,
            claim_type="policy_statement",
            text=text,
            passage=passage,
            passage_start=span[0] if span else None,
            passage_end=span[1] if span else None,
            policy_series=series,
            stated_value=value,
            stated_unit=TARIFF_UNIT,
            direction=direction,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            time_precision=precision,
            place_candidates=[],
            extractor_version=EXTRACTOR_VERSION,
            valid=True,
        )


def reconcile_tariff_claims(session: Session, document: EvidenceDocument, text: str) -> int:
    """Invalidate tariff claims whose ``stated_value`` the code cannot find in the document.

    Applies to valid ``policy_statement`` claims on an electricity tariff series made by anything
    other than this adapter (that is, by the language model). Returns how many it invalidated.
    Call it after claims are stored, with the same text the extraction read.
    """
    figures = tariff_values(text)
    invalidated = 0
    claims = session.scalars(
        select(Claim).where(
            Claim.evidence_document_id == document.id,
            Claim.claim_type == "policy_statement",
            Claim.valid.is_(True),
            Claim.policy_series.like("electricity_tariff%"),
            Claim.stated_value.is_not(None),
            Claim.extractor_version != EXTRACTOR_VERSION,
        )
    )
    for claim in claims:
        if claim.stated_value not in figures:
            claim.valid = False
            claim.invalid_reason = MISMATCH
            invalidated += 1
    session.flush()
    return invalidated


register_adapter("nerc", NercAdapter())
