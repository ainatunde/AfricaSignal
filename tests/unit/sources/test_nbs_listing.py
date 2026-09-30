"""The eLibrary listing and report-page parsing, against rows copied from the real site."""

from datetime import date

import pytest

from africasignal.catalog import load_items
from africasignal.sources.nbs import excel_link, parse_listing, title_month
from tests.unit.sources.nbs_fixtures import FIXTURES, manifest

BASE = "https://nigerianstat.gov.ng/elibrary"


@pytest.fixture(scope="module")
def entries():  # type: ignore[no-untyped-def]
    html = (FIXTURES / "elibrary_listing_rows.html").read_text(encoding="utf-8")
    return parse_listing(html, BASE)


def test_every_real_row_is_read_with_title_release_date_and_report_page(entries) -> None:  # type: ignore[no-untyped-def]
    assert len(entries) == 14
    by_title = {e.title: e for e in entries}
    petrol = by_title["Premium Motor Spirit (Petrol) Price Watch (October 2024)"]
    assert petrol.published == date(2024, 11, 19)
    assert petrol.read_url == "https://nigerianstat.gov.ng/elibrary/read/1241584"
    assert by_title["Selected Food Prices Watch (October 2024)"].published == date(2024, 11, 24)


def test_html_comments_inside_rows_are_not_read_as_cells(entries) -> None:  # type: ignore[no-untyped-def]
    # Each real row has a commented-out <td> with an image before the title cell.
    assert all(not e.title.startswith("<") and "avatar" not in e.title for e in entries)


def test_a_double_space_in_a_real_title_is_collapsed(entries) -> None:  # type: ignore[no-untyped-def]
    assert "Selected Food Prices Watch (August 2024)" in {e.title for e in entries}


def test_the_ten_saved_files_match_ten_listing_rows(entries) -> None:  # type: ignore[no-untyped-def]
    by_title = {e.title: e for e in entries}
    for f in manifest()["files"]:
        entry = by_title[f["listing_title"]]
        assert entry.read_url == f["report_page"]
        assert entry.published.strftime("%a %b %d %Y") == f["listing_release_date"]


def test_tracked_publications_are_recognised_and_others_are_not(entries) -> None:  # type: ignore[no-untyped-def]
    catalog = load_items()
    tracked = [e for e in entries if catalog.publication_for_title(e.title)]
    assert len(tracked) == 12  # ten saved files plus August food and August cooking gas
    ignored = {e.title for e in entries} - {e.title for e in tracked}
    assert ignored == {
        "Transport Fare Watch (October 2024)",
        "Nigeria General Household Survey - Panel (GHS-Panel) Wave 5 (2023/2024)",
    }


@pytest.mark.parametrize(
    ("title", "month"),
    [
        ("Premium Motor Spirit (Petrol) Price Watch (October 2024)", date(2024, 10, 1)),
        ("Selected Food Prices Watch (August 2024)", date(2024, 8, 1)),
        ("Selected Food Prices Watch ( September  2024 )", date(2024, 9, 1)),
        ("Nigeria General Household Survey - Panel (GHS-Panel) Wave 5 (2023/2024)", None),
        ("Petroleum Products Distribution Statistics Full Year 2023", None),
        ("Something (Octobre 2024)", None),
    ],
)
def test_reference_month_comes_from_the_title(title: str, month: date | None) -> None:
    assert title_month(title) == month


def test_the_excel_link_is_the_download_tables_link_not_the_pdf() -> None:
    fragment = (FIXTURES / "report_page_download_links.html").read_text(encoding="utf-8")
    assert (
        excel_link(fragment, "https://nigerianstat.gov.ng/elibrary/read/1241584")
        == "https://nigerianstat.gov.ng/resource/PMS_OCT_2024_REPORT.xlsx"
    )


def test_a_report_page_with_only_a_pdf_has_no_excel_link() -> None:
    page = '<a title="download" href="https://nigerianstat.gov.ng/download/1241000">Report</a>'
    assert excel_link(page, "https://nigerianstat.gov.ng/elibrary/read/1241000") is None


def test_relative_links_are_resolved_against_the_page() -> None:
    page = '<a href="/resource/X.xlsx">Tables</a>'
    assert (
        excel_link(page, "https://nigerianstat.gov.ng/elibrary/read/1")
        == "https://nigerianstat.gov.ng/resource/X.xlsx"
    )


def test_a_page_that_is_not_the_listing_yields_no_entries() -> None:
    assert parse_listing("<html><body><h1>Maintenance</h1></body></html>", BASE) == []
