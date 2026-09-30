"""The 2025-onward NBS releases from the microdata catalog (ZIPs holding a PDF and an xlsx).

The "site" serves the real petrol download list; the other four catalog pages are built from the
manifest's real link texts and URLs. Each ZIP is rebuilt from the real workbook plus a stub PDF, with
the file dates NBS put in the real ZIPs (the release date is read from them).
"""

from __future__ import annotations

import functools
import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from africasignal.evidence.capture import capture, record_document
from africasignal.jobs import handlers, queue
from africasignal.jobs.handlers import assess_situation as assess_situation_module
from africasignal.jobs.handlers import fetch_source as fetch_source_module
from africasignal.jobs.handlers import process_document as process_document_module
from africasignal.jobs.worker import Worker
from africasignal.models import (
    AssessmentVersion,
    EvidenceDocument,
    Measurement,
    Situation,
    Source,
    SourcePermission,
)
from africasignal.net.fetch import FetchResult
from africasignal.publish.situations import assess_situation, ensure_situations
from africasignal.sources import base, nbs
from africasignal.sources.base import AdapterContext
from africasignal.sources.nbs import NbsAdapter
from africasignal.sources.nbs_workbook import NbsParseError
from africasignal.storage import S3Store
from tests.integration.nbs_support import add_places, make_store
from tests.integration.test_nbs_jobs import TABLES
from tests.integration.test_situations import _all_pairs
from tests.unit.sources.nbs_fixtures import FIXTURES, manifest, read

HOME = "https://microdata.nigerianstat.gov.ng/index.php/catalog"
OCTET = "application/octet-stream"
ENTRIES = {e["publication"]: e for e in manifest()["microdata_files"]}
PETROL_FRAGMENT = (FIXTURES / "microdata_catalog_157_downloads.html").read_text(encoding="utf-8")


def build_zip(entry: dict[str, Any]) -> bytes:
    stamp = date.fromisoformat(entry["latest_member_modified"])
    when = (stamp.year, stamp.month, stamp.day, 12, 0, 0)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(zipfile.ZipInfo("report.pdf", when), b"%PDF-1.7 stub")
        z.writestr(zipfile.ZipInfo(entry["inner_file"], when), read(entry["file"]))
    return out.getvalue()


def catalog_page(entry: dict[str, Any]) -> str:
    if entry["publication"] == "pms":
        return PETROL_FRAGMENT
    return (
        f'<a target="_blank" href="{entry["source_url"]}" class="font-weight-bold">\n'
        f"    {entry['catalog_link_text']}</strong>\n</a>"
    )


class MicrodataSite:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.pages = {entry["catalog_page"]: catalog_page(entry) for entry in ENTRIES.values()}
        self.zips = {e["source_url"]: build_zip(e) for e in ENTRIES.values()}
        self.broken: set[str] = set()

    def __call__(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append((url, kwargs))
        if url in self.zips:
            body = b"<html>maintenance</html>" if url in self.broken else self.zips[url]
            return FetchResult(
                url=url, status_code=200, headers={"content-type": OCTET}, content=body
            )
        if url in self.pages:
            return FetchResult(
                url=url,
                status_code=200,
                headers={"content-type": "text/html"},
                content=self.pages[url].encode(),
            )
        return FetchResult(url=url, status_code=404)


def add_microdata_source(session: Session) -> Source:
    source = Source(
        slug="nbs-microdata",
        name="National Bureau of Statistics (microdata catalog)",
        kind="official_statistics",
        adapter="nbs",
        owner="National Bureau of Statistics",
        home_url=HOME,
        schedule_minutes=1440,
        max_requests_per_hour=30,
    )
    session.add(source)
    session.flush()
    session.add(
        SourcePermission(
            source_id=source.id,
            version=1,
            may_collect=True,
            may_store_full_text=True,
            may_republish_numbers=True,
            approved_at=datetime.now(UTC),
        )  # fmt: skip
    )
    session.flush()
    return source


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from make_store()


@pytest.fixture
def site() -> MicrodataSite:
    return MicrodataSite()


@pytest.fixture
def source(session: Session) -> Source:
    add_places(session)
    return add_microdata_source(session)


def _ctx(session: Session, store: S3Store) -> AdapterContext:
    return AdapterContext(session=session, store=store)


def _zip_document(
    session: Session, store: S3Store, source: Source, publication: str
) -> EvidenceDocument:
    """What process_document would have captured: the ZIP, titled with the catalog link text and
    with no release date (the catalog page has none)."""
    entry = ENTRIES[publication]
    permission = session.scalars(
        select(SourcePermission).where(SourcePermission.source_id == source.id)
    ).one()
    return record_document(
        session, store, source, permission, url=entry["source_url"], content=build_zip(entry),
        content_type=OCTET, title=entry["catalog_link_text"],
    )  # fmt: skip


# --- discovery ---------------------------------------------------------------------------------


def test_discovery_reads_each_publications_catalog_page(
    session: Session, store: S3Store, source: Source, site: MicrodataSite
) -> None:
    items = NbsAdapter(fetch=site).discover(source, _ctx(session, store))
    urls = [u for u, _ in site.calls]
    assert urls == [f"{HOME}/{cid}" for cid in (157, 158, 159, 160, 162)]
    assert all(kw["max_requests_per_hour"] == 30 for _, kw in site.calls)
    # petrol: the three newest downloads of the real list; the others have one each
    assert len(items) == 3 + 4
    petrol = [i for i in items if "/catalog/157/" in i.url]
    assert [i.title for i in petrol] == [
        "PMS Report May 2026",
        "PMS Report April 2026",
        "PMS Report March 2026",
    ]
    assert all(i.published_at is None for i in items)  # the date comes from inside the ZIP
    assert all(i.url.endswith(".zip") for i in items)


def test_only_downloads_not_yet_imported_are_followed(
    session: Session, store: S3Store, source: Source, site: MicrodataSite
) -> None:
    _zip_document(session, store, source, "pms")
    items = NbsAdapter(fetch=site).discover(source, _ctx(session, store))
    assert len(items) == 2 + 4
    assert ENTRIES["pms"]["source_url"] not in {i.url for i in items}


def test_the_number_of_recent_downloads_is_configurable(
    session: Session,
    store: S3Store,
    source: Source,
    site: MicrodataSite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nbs, "RECENT_MONTHS", 1)
    assert len(NbsAdapter(fetch=site).discover(source, _ctx(session, store))) == 5


def test_pdf_only_downloads_are_not_candidates(
    session: Session, store: S3Store, source: Source, site: MicrodataSite
) -> None:
    site.pages[ENTRIES["ago"]["catalog_page"]] = (
        '<a target="_blank" href="https://microdata.nigerianstat.gov.ng/index.php/catalog/158/'
        'download/9/AGO_Report.pdf" class="font-weight-bold">\n AGO PDF</strong></a>'
    )
    items = NbsAdapter(fetch=site).discover(source, _ctx(session, store))
    assert not [i for i in items if "/catalog/158/" in i.url]


def test_catalog_pages_with_no_downloads_mean_the_layout_changed(
    session: Session, store: S3Store, source: Source, site: MicrodataSite
) -> None:
    for page in site.pages:
        site.pages[page] = "<html><body>Something else</body></html>"
    with pytest.raises(NbsParseError, match="no downloads found"):
        NbsAdapter(fetch=site).discover(source, _ctx(session, store))


def test_a_download_hosted_elsewhere_is_skipped(
    session: Session, store: S3Store, source: Source, site: MicrodataSite
) -> None:
    site.pages[ENTRIES["ago"]["catalog_page"]] = (
        '<a target="_blank" href="https://files.example.net/AGO.zip" class="font-weight-bold">\n'
        " AGO</strong></a>"
    )
    items = NbsAdapter(fetch=site).discover(source, _ctx(session, store))
    assert len(items) == 3 + 3
    assert not any("example.net" in i.url for i in items)


# --- processing --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("publication", "rows", "released"),
    [
        ("pms", 114, date(2026, 6, 24)),
        ("ago", 114, date(2026, 6, 24)),
        ("dpk", 114, date(2026, 6, 24)),
        ("lpg", 228, date(2026, 5, 26)),  # 5kg and 12.5kg
        ("food", 9, date(2026, 6, 25)),  # beans, garri and maize: national, 3 months each
    ],
)
def test_a_zip_is_unpacked_and_its_workbook_imported_with_the_release_date_from_the_zip(
    session: Session, store: S3Store, source: Source, publication: str, rows: int, released: date
) -> None:
    doc = _zip_document(session, store, source, publication)
    assert doc.mime == "application/zip" and doc.published_at is None
    result = NbsAdapter().process(doc, _ctx(session, store))
    assert result.measurements == rows
    assert doc.published_at == datetime(released.year, released.month, released.day, tzinfo=UTC)
    vintages = set(session.scalars(select(Measurement.vintage)))
    assert vintages == {released}
    assert doc.origin_id is not None


def test_the_publication_comes_from_the_catalog_id_in_the_download_url(
    session: Session, store: S3Store, source: Source
) -> None:
    doc = _zip_document(session, store, source, "ago")
    assert "/catalog/158/" in doc.url and doc.title == "AGO Report May 2026"
    NbsAdapter().process(doc, _ctx(session, store))  # would fail if it were not found as AGO
    assert session.scalar(select(func.count()).select_from(Measurement)) == 114


def test_a_zip_holding_no_workbook_fails_the_import(
    session: Session, store: S3Store, source: Source
) -> None:
    permission = session.scalars(
        select(SourcePermission).where(SourcePermission.source_id == source.id)
    ).one()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("report.pdf", b"%PDF")
    doc = record_document(
        session, store, source, permission, url=f"{HOME}/157/download/1/x.zip", content=out.getvalue(),
        content_type=OCTET, title="PMS Report June 2026",
    )  # fmt: skip
    with pytest.raises(NbsParseError, match="no Excel file"):
        NbsAdapter().process(doc, _ctx(session, store))


def test_may_2026_petrol_assessments_are_reported_in_july_and_stale_by_october(
    session: Session, store: S3Store, source: Source
) -> None:
    NbsAdapter().process(_zip_document(session, store, source, "pms"), _ctx(session, store))
    found = {s.slug: s for s in ensure_situations(session, _all_pairs(session, "pms_litre"))}
    july = datetime(2026, 7, 1, tzinfo=UTC)
    lagos = assess_situation(session, found["price-pms_litre-ng-la"].id, july).version
    assert lagos is not None
    assert lagos.headline == (
        "Average petrol (PMS) price in Lagos State rose 5.0% in May 2026 to ₦1,561.22 (NBS)"
    )
    assert (lagos.evidence_state, lagos.severity) == ("reported", "medium")  # +45.0 % on the year
    national = assess_situation(session, found["price-pms_litre-ng"].id, july).version
    assert national is not None and national.headline == (
        "Average petrol (PMS) price in Nigeria rose 4.1% in May 2026 to ₦1,596.25 (NBS)"
    )
    # The same inputs assessed first in October are past the 120-day limit: insufficient.
    october = datetime(2026, 10, 15, tzinfo=UTC)
    ogun = assess_situation(session, found["price-pms_litre-ng-og"].id, october).version
    assert ogun is not None and ogun.evidence_state == "insufficient" and ogun.severity == "none"


# --- through the job queue ---------------------------------------------------------------------


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield sessionmaker(bind=engine, expire_on_commit=False)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, store: S3Store, site: MicrodataSite) -> None:
    for kind, fn in (
        ("fetch_source", fetch_source_module.fetch_source),
        ("process_document", process_document_module.process_document),
        ("assess_situation", assess_situation_module.assess_situation),
    ):
        monkeypatch.setitem(handlers.HANDLERS, kind, fn)
    monkeypatch.setattr(base, "ADAPTERS", {"nbs": NbsAdapter(fetch=site)})
    for module in (fetch_source_module, process_document_module):
        monkeypatch.setattr(module, "get_adapter", lambda name: base.ADAPTERS.get(name))
        monkeypatch.setattr(module, "get_store", lambda: store)
    monkeypatch.setattr(process_document_module, "capture", functools.partial(capture, fetch=site))
    monkeypatch.setattr(nbs, "RECENT_MONTHS", 1)


def test_fetching_the_microdata_source_imports_the_latest_release_of_every_publication(
    factory: sessionmaker[Session], wired: None, site: MicrodataSite
) -> None:
    with factory() as s:
        add_places(s)
        source = add_microdata_source(s)
        s.commit()
        source_id = source.id
    with factory() as s:
        queue.enqueue(s, "fetch_source", {"source_id": source_id})
        s.commit()
    worker = Worker(factory, "w")
    ran = 0
    while ran < 600 and worker.run_once():
        ran += 1

    with factory() as s:
        docs = s.scalars(select(EvidenceDocument)).all()
        assert {d.title for d in docs} == {e["catalog_link_text"] for e in ENTRIES.values()}
        assert all(d.mime == "application/zip" and d.published_at for d in docs)
        assert s.scalar(select(func.count()).select_from(Measurement)) == 3 * 114 + 228 + 9
        assert s.scalar(select(func.count()).select_from(Situation)) == 3 * 38 + 2 * 38 + 3
        versions = s.scalars(select(AssessmentVersion)).all()
        assert len(versions) == 3 * 38 + 2 * 38 + 3
        assert {v.status for v in versions} == {"draft"}
        src = s.get(Source, source_id)
        assert src is not None and src.health == "healthy" and src.last_error is None
        states = s.execute(text("SELECT status, count(*) FROM job GROUP BY 1")).all()
        assert dict(states) == {"done": ran}  # nothing failed or waits

    # A second run finds every download already imported and fetches only the five catalog pages.
    before = len(site.calls)
    with factory() as s:
        queue.enqueue(s, "fetch_source", {"source_id": source_id})
        s.commit()
    while worker.run_once():
        pass
    assert len(site.calls) == before + 5
