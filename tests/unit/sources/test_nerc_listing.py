from datetime import UTC, datetime

from africasignal.sources.nerc import DISCO_SLUGS, disco_code, parse_listing
from tests.unit.sources.nerc_fixtures import NERC

BASE = "https://nerc.gov.ng/resource-category/orders/"


def listing():  # type: ignore[no-untyped-def]
    return parse_listing((NERC / "orders.html").read_text(encoding="utf-8"), BASE)


def test_the_saved_listing_has_ten_orders_each_with_a_pdf() -> None:
    items = listing()
    assert len(items) == 10
    assert all(i.url.startswith("https://nerc.gov.ng/wp-content/uploads/2026/09/") for i in items)
    assert all(i.url.endswith(".pdf") for i in items)


def test_titles_are_unescaped_and_dates_read() -> None:
    first, second = listing()[:2]
    assert first.title.startswith("Revised Order on Successor Distribution Companies’ (“DisCos”)")
    assert first.published_at == datetime(2026, 9, 9, tzinfo=UTC)
    assert second.title == "YEDC MYTO SEPTEMBER 2026"
    assert second.published_at == datetime(2026, 9, 2, tzinfo=UTC)


def test_every_monthly_order_in_the_listing_is_matched_to_a_disco() -> None:
    codes = [disco_code(i.title, i.url) for i in listing()]
    assert codes[0] is None  # the OpEx order is not a DisCo schedule
    assert sorted(c for c in codes if c) == sorted(
        ["YEDC", "PHED", "KEDCO", "KAEDC", "JED", "IE", "IBEDC", "EKEDP", "EEDC"]
    )


def test_a_disco_is_found_from_the_file_name_when_the_title_is_unhelpful() -> None:
    url = "https://nerc.gov.ng/wp-content/uploads/2026/09/EKEDP_HOLDCO_YSS_September_2026_091.pdf"
    assert disco_code("September 2026 Supplementary Order", url) == "EKEDP"
    assert disco_code("September 2026 Supplementary Order", "https://nerc.gov.ng/a.pdf") is None


def test_an_unknown_abbreviation_is_not_a_disco() -> None:
    assert disco_code("XYZ MYTO SEPTEMBER 2026", "https://nerc.gov.ng/a.pdf") is None


def test_an_entry_without_a_pdf_is_left_out() -> None:
    html = (
        '<div class="publication"><h6 class="title">No file</h6>'
        '<a href="#" title="September 9, 2026">x</a></div>'
        '<div class="publication"><h6 class="title">With file</h6>'
        '<a href="#" title="September 8, 2026">x</a><a href="/files/a.pdf">View</a></div>'
    )
    [item] = parse_listing(html, "https://nerc.gov.ng/")
    assert (item.title, item.url) == ("With file", "https://nerc.gov.ng/files/a.pdf")


def test_the_disco_table_has_unique_slugs() -> None:
    assert len(set(DISCO_SLUGS.values())) == len(DISCO_SLUGS)
