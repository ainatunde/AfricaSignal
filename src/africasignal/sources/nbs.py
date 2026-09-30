"""NBS price-watch adapter (spec B6.3, AS-010).

Discovery reads the NBS eLibrary listing (one large HTML table), keeps the price-watch rows of the
latest few months and, for the ones not yet imported, finds the Excel link on each report's page.
Processing parses the workbook (``nbs_workbook``) and stores measurements.

Storing rules, per (item, place, month):
* nothing exists yet: insert, with the release date as the vintage;
* the same value is already current: nothing to do (each release repeats the previous month and
  the same month a year earlier, so most values arrive again);
* a different value from a later release: insert a new vintage and point the old row at it
  (``superseded_by_id``): a restatement, which later invalidates assessments that used the old
  value;
* a different value from an earlier release than the current one: insert it as history, already
  superseded, so importing releases out of order gives the same result;
* a different value in the same release (vintage): not inserted and reported as a conflict.
Reference-month values far from the previous month's median wait in ``measurement_review``.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.catalog import Item, NbsPublication, load_items
from africasignal.models import (
    EvidenceDocument,
    Measurement,
    MeasurementReview,
    Place,
    ReportingOrigin,
    Series,
    Source,
)
from africasignal.net.fetch import FetchResult, fetch_document
from africasignal.places.resolve import resolve_place
from africasignal.sources.base import (
    AdapterContext,
    DiscoveredItem,
    ProcessResult,
    register_adapter,
)
from africasignal.sources.nbs_workbook import (
    Column,
    NbsParseError,
    ParsedTable,
    ParsedWorkbook,
    TableRow,
    parse_workbook,
)
from africasignal.storage import ObjectStore

log = logging.getLogger("africasignal.sources.nbs")

# How many months of releases per publication discovery follows, counted back from the newest
# release in the listing. Older months are already covered by the comparison columns.
RECENT_MONTHS = 3
LISTING_MAX_BYTES = 8_000_000  # the eLibrary page was 1.9 MB when this was written
# A reference-month value outside [LOW, HIGH] x the previous month's median is queued for review.
RANGE_LOW = Decimal("0.2")
RANGE_HIGH = Decimal("5")
NATIONAL_CODE = "NG"

_MONTH_IN_TITLE = re.compile(r"\(\s*([A-Za-z]+)\s+(\d{4})\s*\)")
_LISTING_DATE = re.compile(r"^[A-Z][a-z]{2} [A-Z][a-z]{2} \d{2} \d{4}$")
_READ_LINK = re.compile(r"/elibrary/read/(\d+)")
_EXCEL_LINK = re.compile(r'href="([^"]+\.xlsx?)"', re.IGNORECASE)
_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]  # fmt: skip

Fetcher = Callable[..., FetchResult]


@dataclass(frozen=True)
class ListingEntry:
    title: str
    published: date  # the release date shown in the listing: the measurement vintage
    read_url: str

    @property
    def reference_month(self) -> date | None:
        return title_month(self.title)


def title_month(title: str) -> date | None:
    """The reference month in a listing title such as "... Price Watch (October 2024)"."""
    match = _MONTH_IN_TITLE.search(title)
    if match is None:
        return None
    name = match.group(1).lower()
    if name not in _MONTH_NAMES:
        return None
    return date(int(match.group(2)), _MONTH_NAMES.index(name) + 1, 1)


class _ListingParser(HTMLParser):
    """Collects the text of every table row's cells and the first report link in the row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[list[str], str | None]] = []
        self._cells: list[str] | None = None
        self._link: str | None = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._cells, self._link = [], None
        elif tag == "td" and self._cells is not None:
            self._cells.append("")
            self._in_cell = True
        elif tag == "a" and self._cells is not None and self._link is None:
            href = dict(attrs).get("href") or ""
            if _READ_LINK.search(href):
                self._link = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self._in_cell = False
        elif tag == "tr" and self._cells is not None:
            self.rows.append(([re.sub(r"\s+", " ", c).strip() for c in self._cells], self._link))
            self._cells = None

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._cells:
            self._cells[-1] += data


def parse_listing(html: str, base_url: str = "") -> list[ListingEntry]:
    """Every eLibrary row with a title, a release date and a report page."""
    parser = _ListingParser()
    parser.feed(html)
    entries = []
    for cells, link in parser.rows:
        if link is None or not cells or not cells[0]:
            continue
        stamps = [c for c in cells if _LISTING_DATE.match(c)]
        if not stamps:
            continue
        published = datetime.strptime(stamps[0], "%a %b %d %Y").date()
        entries.append(ListingEntry(cells[0], published, urljoin(base_url, link)))
    return entries


def excel_link(read_page_html: str, page_url: str) -> str | None:
    """The workbook link on a report page ("Download Tables"), or None if it only has a PDF."""
    match = _EXCEL_LINK.search(read_page_html)
    return urljoin(page_url, match.group(1)) if match else None


# --------------------------------------------------------------------------------------------
# Importing a parsed workbook
# --------------------------------------------------------------------------------------------


@dataclass
class NbsImport:
    """What an import did, for the job log, the console and the tests."""

    reference_month: date
    inserted: int = 0
    revised: int = 0
    historic: int = 0
    unchanged: int = 0
    queued_for_review: int = 0
    rejected: int = 0
    unresolved_places: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # (item_code, place_id) pairs whose current data changed: assessments to refresh.
    touched: set[tuple[str, int]] = field(default_factory=set)

    @property
    def measurements(self) -> int:
        return self.inserted + self.revised + self.historic

    def as_result(self) -> ProcessResult:
        return ProcessResult(measurements=self.measurements, notes=self.notes)


class _Places:
    """Place lookups for one import: the country, and NBS state names resolved once each."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._states: dict[str, int | None] = {}
        self.country_id = session.scalars(
            select(Place.id).where(Place.code == NATIONAL_CODE)
        ).one_or_none()

    def state(self, name: str) -> int | None:
        key = name.strip().lower()
        if key not in self._states:
            found = resolve_place(self.session, name)
            ok = found.status == "resolved" and found.precision == "state"
            self._states[key] = found.place_id if ok else None
        return self._states[key]


def _month_end(month: date) -> date:
    nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def _series_for(session: Session, source: Source, item: Item) -> Series:
    series = session.scalars(
        select(Series).where(Series.item_code == item.code, Series.source_id == source.id)
    ).one_or_none()
    if series is None:
        series = Series(
            item_code=item.code,
            topic=item.topic,
            source_id=source.id,
            unit=item.unit,
            currency=item.currency,
            frequency=item.frequency,
        )
        session.add(series)
        session.flush()
    return series


Outcome = Literal["inserted", "revised", "historic", "unchanged", "conflict"]


def store_measurement(
    session: Session,
    series: Series,
    place_id: int,
    month: date,
    value: Decimal,
    vintage: date,
    document: EvidenceDocument,
) -> Outcome:
    """Insert one value following the rules in the module docstring."""
    rows = list(
        session.scalars(
            select(Measurement).where(
                Measurement.series_id == series.id,
                Measurement.place_id == place_id,
                Measurement.period_start == month,
            )
        )
    )
    same_release = next((m for m in rows if m.vintage == vintage), None)
    if same_release is not None:
        return "unchanged" if same_release.value == value else "conflict"
    current = max(
        (m for m in rows if m.superseded_by_id is None), key=lambda m: m.vintage, default=None
    )
    if current is not None and current.value == value:
        return "unchanged"

    row = Measurement(
        series_id=series.id,
        place_id=place_id,
        period_start=month,
        period_end=_month_end(month),
        value=value,
        vintage=vintage,
        evidence_document_id=document.id,
    )
    if current is not None and current.vintage > vintage:
        row.superseded_by_id = current.id  # older than what we hold: history
        session.add(row)
        session.flush()
        return "historic"
    session.add(row)
    session.flush()
    if current is None:
        return "inserted"
    current.superseded_by_id = row.id
    return "revised"


def _baseline(table: ParsedTable | None, row: TableRow) -> Decimal | None:
    """The previous month's median across states (or, for a national-only item, its own
    previous-month value): what a reference-month value is checked against."""
    if table is not None and table.rows and table.national is not None:
        previous = [r.values["previous"] for r in table.rows if r.values["previous"] is not None]
        return statistics.median(previous) if previous else None
    return row.values["previous"]


def _in_range(value: Decimal, baseline: Decimal | None) -> bool:
    if baseline is None or baseline <= 0:
        return True  # nothing to compare with
    return baseline * RANGE_LOW <= value <= baseline * RANGE_HIGH


def _import_row(
    session: Session,
    result: NbsImport,
    *,
    series: Series,
    place_id: int,
    label: str,
    row: TableRow,
    months: dict[Column, date],
    baseline: Decimal | None,
    vintage: date,
    document: EvidenceDocument,
) -> None:
    for column, month in months.items():
        value = row.values[column]
        if value is None:
            continue
        if value <= 0:
            result.rejected += 1
            result.notes.append(
                f"{series.item_code} {label} {month:%Y-%m}: {value} is not positive"
            )
            continue
        if column == "reference" and not _in_range(value, baseline):
            existing = session.scalars(
                select(MeasurementReview.id).where(
                    MeasurementReview.series_id == series.id,
                    MeasurementReview.place_id == place_id,
                    MeasurementReview.period_start == month,
                    MeasurementReview.vintage == vintage,
                )
            ).first()
            if existing is None:
                assert baseline is not None
                session.add(
                    MeasurementReview(
                        series_id=series.id,
                        place_id=place_id,
                        period_start=month,
                        period_end=_month_end(month),
                        value=value,
                        vintage=vintage,
                        evidence_document_id=document.id,
                        reference_value=baseline,
                        reason=(
                            f"{value} is outside {RANGE_LOW}x to {RANGE_HIGH}x of the previous "
                            f"month's median ({baseline})"
                        ),
                    )
                )
                session.flush()
                result.queued_for_review += 1
                result.notes.append(f"{series.item_code} {label}: {value} queued for review")
            continue
        outcome = store_measurement(session, series, place_id, month, value, vintage, document)
        if outcome == "conflict":
            result.notes.append(
                f"{series.item_code} {label} {month:%Y-%m}: release {vintage} already holds a "
                f"different value; not changed"
            )
            continue
        setattr(result, outcome, getattr(result, outcome) + 1)
        if outcome in ("inserted", "revised"):
            result.touched.add((series.item_code, place_id))
            if outcome == "revised":
                result.notes.append(f"{series.item_code} {label} {month:%Y-%m} revised to {value}")


def _origin(session: Session, document: EvidenceDocument, label: str) -> None:
    if document.origin_id is not None:
        return
    origin = ReportingOrigin(
        kind="official_dataset", label=label, first_seen_at=document.retrieved_at
    )
    session.add(origin)
    session.flush()
    document.origin_id = origin.id


def import_parsed(
    session: Session,
    source: Source,
    document: EvidenceDocument,
    parsed: ParsedWorkbook,
    vintage: date,
) -> NbsImport:
    """Store the measurements in a parsed workbook for the items ``items.yaml`` maps to it."""
    items = load_items().items_for(parsed.publication)
    result = NbsImport(reference_month=parsed.reference_month)
    places = _Places(session)
    if places.country_id is None:
        raise NbsParseError("the country place NG is not loaded; run the places loader first")

    matched = 0
    for item in items:
        if parsed.publication == "food":
            items_table = parsed.tables[0]
            wanted = {label.lower() for label in item.nbs.labels}
            rows = [r for r in items_table.rows if r.name.lower() in wanted]
            if not rows:
                result.notes.append(f"{item.code}: no row labelled {item.nbs.labels} in the file")
                continue
            matched += 1
            series = _series_for(session, source, item)
            for row in rows:
                _import_row(
                    session, result, series=series, place_id=places.country_id, label=row.name,
                    row=row, months=items_table.months, baseline=_baseline(None, row),
                    vintage=vintage, document=document,
                )  # fmt: skip
            continue

        table = parsed.table(item.nbs.block) if item.nbs.block else parsed.tables[0]
        if table is None:
            result.notes.append(f"{item.code}: block {item.nbs.block} not found in the file")
            continue
        matched += 1
        series = _series_for(session, source, item)
        for row in table.rows:
            place_id = places.state(row.name)
            if place_id is None:
                if row.name not in result.unresolved_places:
                    result.unresolved_places.append(row.name)
                continue
            _import_row(
                session, result, series=series, place_id=place_id, label=row.name, row=row,
                months=table.months, baseline=_baseline(table, row), vintage=vintage,
                document=document,
            )  # fmt: skip
        if table.national is not None:
            _import_row(
                session, result, series=series, place_id=places.country_id,
                label=table.national.name, row=table.national, months=table.months,
                baseline=_baseline(table, table.national), vintage=vintage, document=document,
            )  # fmt: skip
        result.notes.extend(table.warnings)

    if items and matched == 0:
        raise NbsParseError(
            f"none of the {len(items)} tracked {parsed.publication} items were found in the file"
        )
    if result.unresolved_places:
        result.notes.append(f"state names not resolved to a place: {result.unresolved_places}")
    return result


def import_workbook(
    session: Session,
    store: ObjectStore,
    source: Source,
    document: EvidenceDocument,
    publication: NbsPublication,
    *,
    vintage: date,
    expected_month: date | None = None,
) -> NbsImport:
    """Parse the stored workbook of ``document`` and store its measurements."""
    parsed = parse_workbook(store.get(document.storage_key), publication)
    if expected_month is not None and parsed.reference_month != expected_month:
        raise NbsParseError(
            f"the file's reference month is {parsed.reference_month:%B %Y} but the release is "
            f"for {expected_month:%B %Y}"
        )
    label = document.title or f"NBS {publication.code} price watch, {parsed.reference_month:%B %Y}"
    _origin(session, document, label)
    result = import_parsed(session, source, document, parsed, vintage)
    log.info(
        "nbs import %s %s: %d inserted, %d revised, %d historic, %d unchanged, %d for review",
        publication.code,
        parsed.reference_month,
        result.inserted,
        result.revised,
        result.historic,
        result.unchanged,
        result.queued_for_review,
    )
    return result


# --------------------------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------------------------


class NbsAdapter:
    slug_prefix = "nbs"

    def __init__(self, fetch: Fetcher = fetch_document) -> None:
        self._fetch = fetch

    def _get(self, source: Source, url: str, **kwargs: object) -> FetchResult:
        result = self._fetch(url, max_requests_per_hour=source.max_requests_per_hour, **kwargs)
        if not result.success:
            raise NbsParseError(
                result.error or f"fetch of {url} returned HTTP {result.status_code}"
            )
        return result

    def discover(self, source: Source, ctx: AdapterContext) -> list[DiscoveredItem]:
        if not source.home_url:
            raise NbsParseError(f"source {source.slug} has no home_url")
        listing = self._get(source, source.home_url, max_bytes=LISTING_MAX_BYTES)
        entries = parse_listing(listing.content.decode("utf-8", errors="replace"), listing.url)
        catalog = load_items()
        tracked = [
            (e, p)
            for e in entries
            if (p := catalog.publication_for_title(e.title)) is not None
            and e.reference_month is not None
        ]
        if not tracked:
            raise NbsParseError(
                f"the eLibrary listing ({len(entries)} rows) has no price-watch rows; "
                "the page layout may have changed"
            )

        newest: dict[str, date] = {}
        for entry, pub in tracked:
            assert entry.reference_month is not None
            newest[pub.code] = max(
                newest.get(pub.code, entry.reference_month), entry.reference_month
            )
        floor = {code: _months_back(month, RECENT_MONTHS - 1) for code, month in newest.items()}
        wanted = [e for e, p in tracked if e.reference_month and e.reference_month >= floor[p.code]]
        known = set(
            ctx.session.scalars(
                select(EvidenceDocument.title).where(EvidenceDocument.source_id == source.id)
            )
        )

        items: list[DiscoveredItem] = []
        for entry in sorted(wanted, key=lambda e: (e.published, e.title)):
            if entry.title in known:
                continue
            page = self._get(source, entry.read_url)
            link = excel_link(page.content.decode("utf-8", errors="replace"), page.url)
            if link is None:
                log.warning("nbs: %r has no Excel download; skipped", entry.title)
                continue
            if not _same_site(link, source.home_url):
                log.warning("nbs: %r links to another site (%s); skipped", entry.title, link)
                continue
            items.append(
                DiscoveredItem(
                    url=link,
                    title=entry.title,
                    published_at=datetime(
                        entry.published.year,
                        entry.published.month,
                        entry.published.day,
                        tzinfo=UTC,
                    ),
                )  # fmt: skip
            )
        return items

    def process(self, doc: EvidenceDocument, ctx: AdapterContext) -> ProcessResult:
        source = ctx.session.get(Source, doc.source_id)
        if source is None:
            raise NbsParseError(f"document {doc.id} has no source")
        title = doc.title or ""
        publication = load_items().publication_for_title(title)
        if publication is None:
            raise NbsParseError(f"{title!r} is not a tracked NBS price watch")
        if doc.published_at is None:
            raise NbsParseError(f"document {doc.id} has no release date to use as its vintage")
        return import_workbook(
            ctx.session,
            ctx.store,
            source,
            doc,
            publication,
            vintage=doc.published_at.date(),
            expected_month=title_month(title),
        ).as_result()


def _months_back(month: date, n: int) -> date:
    index = month.year * 12 + (month.month - 1) - n
    return date(index // 12, index % 12 + 1, 1)


def _same_site(url: str, home_url: str) -> bool:
    host, home = urlparse(url).hostname or "", urlparse(home_url).hostname or ""
    return host == home or host.endswith("." + home)


register_adapter("nbs", NbsAdapter())
