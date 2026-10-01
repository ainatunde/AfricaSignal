"""NBS discovery and processing through the adapter interface (AS-010), against PostgreSQL.

The "site" is a fake fetcher that serves the real listing rows saved in ``tests/fixtures/nbs``.
Report pages are the real download-links fragment with the workbook URL of each report; the two
August rows have no saved workbook, so their pages point at made-up file names.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from africasignal.catalog import load_items
from africasignal.models import Source
from africasignal.net.fetch import FetchResult
from africasignal.sources import nbs
from africasignal.sources.base import AdapterContext
from africasignal.sources.nbs import NbsAdapter, parse_listing
from africasignal.sources.nbs_workbook import NbsParseError
from africasignal.storage import S3Store
from tests.integration.nbs_support import add_places, add_source, import_bytes, make_store
from tests.unit.sources.nbs_fixtures import FIXTURES, manifest

LISTING_URL = "https://nigerianstat.gov.ng/elibrary"
LISTING = (FIXTURES / "elibrary_listing_rows.html").read_text(encoding="utf-8")
FRAGMENT = (FIXTURES / "report_page_download_links.html").read_text(encoding="utf-8")
REAL_LINK = "https://nigerianstat.gov.ng/resource/PMS_OCT_2024_REPORT.xlsx"


class FakeSite:
    """Serves the listing and one report page per listing row; records every request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.listing = LISTING
        self.workbook_links = {f["report_page"]: f["source_url"] for f in manifest()["files"]}
        self.pages: dict[str, str] = {}  # overrides: url -> html
        self.failing: set[str] = set()

    def _page(self, url: str) -> str:
        if url in self.pages:
            return self.pages[url]
        link = self.workbook_links.get(url)
        if link is None:  # an August row: no saved workbook
            link = f"https://nigerianstat.gov.ng/resource/made_up_{url.rsplit('/', 1)[-1]}.xlsx"
        return FRAGMENT.replace(REAL_LINK, link)

    def __call__(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append((url, kwargs))
        if url in self.failing:
            return FetchResult(url=url, error="connection reset")
        body = self.listing if url == LISTING_URL else self._page(url)
        return FetchResult(
            url=url,
            status_code=200,
            headers={"content-type": "text/html"},
            content=body.encode(),
        )

    @property
    def report_pages_fetched(self) -> list[str]:
        return [u for u, _ in self.calls if u != LISTING_URL]


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from make_store()


@pytest.fixture
def source(session: Session) -> Source:
    add_places(session)
    return add_source(session)


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


def _ctx(session: Session, store: S3Store) -> AdapterContext:
    return AdapterContext(session=session, store=store)


def test_discovery_finds_the_excel_link_of_every_tracked_release_in_the_window(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    items = NbsAdapter(fetch=site).discover(source, _ctx(session, store))
    assert len(items) == 12  # ten saved files and the August food and cooking gas reports
    by_title = {i.title: i for i in items}
    petrol = by_title["Premium Motor Spirit (Petrol) Price Watch (October 2024)"]
    assert petrol.url == REAL_LINK
    assert petrol.published_at == datetime(2024, 11, 19, tzinfo=UTC)
    assert "Transport Fare Watch (October 2024)" not in by_title
    assert all(i.url.startswith("https://nigerianstat.gov.ng/resource/") for i in items)
    assert [i.published_at for i in items] == sorted(
        i.published_at for i in items if i.published_at
    )


def test_discovery_uses_the_sources_own_rate_limit_and_a_size_cap_for_the_listing(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    NbsAdapter(fetch=site).discover(source, _ctx(session, store))
    assert all(kw["max_requests_per_hour"] == 30 for _, kw in site.calls)
    assert (
        site.calls[0][0] == LISTING_URL and site.calls[0][1]["max_bytes"] == nbs.LISTING_MAX_BYTES
    )
    assert len(site.calls) == 1 + 12


def test_releases_already_imported_are_not_fetched_again(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    import_bytes(session, store, source, "PMS_OCT_2024_REPORT.xlsx")
    items = NbsAdapter(fetch=site).discover(source, _ctx(session, store))
    assert len(items) == 11
    assert "https://nigerianstat.gov.ng/elibrary/read/1241584" not in site.report_pages_fetched
    assert len(site.report_pages_fetched) == 11


def test_only_the_most_recent_months_of_each_publication_are_followed(
    session: Session,
    store: S3Store,
    source: Source,
    site: FakeSite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nbs, "RECENT_MONTHS", 1)
    items = NbsAdapter(fetch=site).discover(source, _ctx(session, store))
    assert sorted(i.title.split("(")[-1] for i in items if i.title) == ["October 2024)"] * 5


def test_the_window_counts_back_from_the_newest_release_not_from_today(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    """NBS's newest release is October 2024, so a window anchored on the current date would be
    empty from 2025 on, and the source would look healthy while importing nothing."""
    items = NbsAdapter(fetch=site).discover(source, _ctx(session, store))
    assert items and max(i.published_at for i in items if i.published_at).year == 2024


def test_a_listing_without_price_watch_rows_means_the_layout_changed(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    site.listing = "<html><body><table><tr><td>Maintenance</td></tr></table></body></html>"
    with pytest.raises(NbsParseError, match="no price-watch rows"):
        NbsAdapter(fetch=site).discover(source, _ctx(session, store))
    rows = re.findall(r"<tr>.*?</tr>", LISTING, re.DOTALL)
    catalog = load_items()
    other = [r for r in rows if not catalog.publication_for_title(parse_listing(r)[0].title)]
    assert len(other) == 2  # the transport fare and household survey rows
    site.listing = "<table>" + "".join(other) + "</table>"
    with pytest.raises(NbsParseError, match=r"listing \(2 rows\) has no price-watch rows"):
        NbsAdapter(fetch=site).discover(source, _ctx(session, store))


def test_an_unreachable_listing_is_an_error_carrying_the_reason(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    site.failing.add(LISTING_URL)
    with pytest.raises(NbsParseError, match="connection reset"):
        NbsAdapter(fetch=site).discover(source, _ctx(session, store))


def test_a_report_without_an_excel_download_is_skipped(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    site.pages["https://nigerianstat.gov.ng/elibrary/read/1241584"] = (
        '<a title="download" href="https://nigerianstat.gov.ng/download/1241584">Report</a>'
    )
    items = NbsAdapter(fetch=site).discover(source, _ctx(session, store))
    assert len(items) == 11
    assert all("Premium Motor Spirit" not in (i.title or "") or "September" in (i.title or "")
               for i in items)  # fmt: skip


def test_a_report_linking_to_another_site_is_skipped(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    site.pages["https://nigerianstat.gov.ng/elibrary/read/1241585"] = FRAGMENT.replace(
        REAL_LINK, "https://files.example.net/DIESEL_OCT_2024_REPORT.xlsx"
    )
    items = NbsAdapter(fetch=site).discover(source, _ctx(session, store))
    assert len(items) == 11
    assert not any("example.net" in i.url for i in items)


def test_a_source_without_a_home_url_cannot_be_discovered(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    source.home_url = None
    with pytest.raises(NbsParseError, match="no home_url"):
        NbsAdapter(fetch=site).discover(source, _ctx(session, store))


def test_process_imports_the_workbook_of_a_captured_document(
    session: Session, store: S3Store, source: Source
) -> None:
    from africasignal.evidence.capture import record_document
    from africasignal.sources.permissions import current_permission
    from tests.integration.nbs_support import XLSX, release
    from tests.unit.sources.nbs_fixtures import read

    info = release("FUEL_SEPT_2024_REPORT.xlsx")
    permission = current_permission(session, source.id)
    assert permission is not None
    doc = record_document(
        session, store, source, permission, url=info["source_url"],
        content=read("FUEL_SEPT_2024_REPORT.xlsx"), content_type=XLSX,
        published_at=datetime(2024, 10, 17, tzinfo=UTC), title=info["listing_title"],
    )  # fmt: skip
    result = NbsAdapter().process(doc, _ctx(session, store))
    assert result.measurements == 114 and result.claims == 0


def test_process_refuses_documents_it_cannot_place(
    session: Session, store: S3Store, source: Source
) -> None:
    from africasignal.evidence.capture import record_document
    from africasignal.sources.permissions import current_permission
    from tests.integration.nbs_support import XLSX
    from tests.unit.sources.nbs_fixtures import read

    permission = current_permission(session, source.id)
    assert permission is not None

    def doc(title: str | None, published: datetime | None):  # type: ignore[no-untyped-def]
        return record_document(
            session, store, source, permission, url=f"https://nigerianstat.gov.ng/resource/{title}.xlsx",
            content=read("FUEL_SEPT_2024_REPORT.xlsx"), content_type=XLSX,
            published_at=published, title=title,
        )  # fmt: skip

    adapter = NbsAdapter()
    with pytest.raises(NbsParseError, match="not a tracked NBS price watch"):
        adapter.process(
            doc("Transport Fare Watch (October 2024)", datetime(2024, 11, 24, tzinfo=UTC)),
            _ctx(session, store),
        )
    with pytest.raises(NbsParseError, match="no release date"):
        adapter.process(
            doc("Premium Motor Spirit (Petrol) Price Watch (September 2024)", None),
            _ctx(session, store),
        )
    with pytest.raises(
        NbsParseError, match="reference month is September 2024 but the release is for October 2024"
    ):
        adapter.process(
            doc(
                "Premium Motor Spirit (Petrol) Price Watch (October 2024)",
                datetime(2024, 11, 19, tzinfo=UTC),
            ),
            _ctx(session, store),
        )


def test_the_saved_listing_parses_to_the_titles_the_adapter_expects() -> None:
    assert len(parse_listing(LISTING, LISTING_URL)) == 14
