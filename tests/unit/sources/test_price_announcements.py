import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from africasignal.sources.price_announcements import (
    AnnouncementError,
    find_price_statements,
    mentions_fuel_price,
    parse_nnpc_posts,
)
from tests.unit.test_fixture_manifests import FIXTURES

HOME = "https://nnpcgroup.com"


def real_feed() -> bytes:
    return (FIXTURES / "nnpc" / "posts_newest.json").read_bytes()


def test_the_saved_feed_gives_ten_posts_with_page_urls_and_dates() -> None:
    posts = parse_nnpc_posts(real_feed(), HOME)
    assert len(posts) == 10
    item, _ = posts[0]
    assert item.url == (
        "https://nnpcgroup.com/insights/"
        "press-release-nnpc-delivers-7-2-trillion-profit-as-production-reaches-multi-year-highs"
    )
    assert item.title.startswith("PRESS RELEASE: NNPC Delivers ₦7.2 Trillion Profit")
    assert item.published_at == datetime(2026, 9, 29, 21, 29, 43, 235000, tzinfo=UTC)


def test_none_of_the_ten_newest_real_posts_is_about_a_fuel_price() -> None:
    posts = parse_nnpc_posts(real_feed(), HOME)
    assert [item.title for item, plain in posts if mentions_fuel_price(plain)] == []


def test_a_feed_that_is_not_the_expected_json_is_an_error() -> None:
    for body in (b"<html>", b'{"data": "x"}', b'{"nope": []}'):
        with pytest.raises(AnnouncementError):
            parse_nnpc_posts(body, HOME)


def test_posts_without_a_slug_or_title_are_skipped() -> None:
    body = json.dumps({"data": [{"title": "x"}, {"slug": "y"}, {"slug": "ok", "title": "Ok"}]})
    assert [i.url for i, _ in parse_nnpc_posts(body.encode(), HOME)] == [f"{HOME}/insights/ok"]


def test_html_tags_are_dropped_from_the_text_used_for_filtering() -> None:
    post = {"slug": "p", "title": "T", "content": "<p>Petrol <b>price</b> is ₦900 per litre</p>"}
    [(_, plain)] = parse_nnpc_posts(json.dumps({"data": [post]}).encode(), HOME)
    assert mentions_fuel_price(plain)


def one(text: str):  # type: ignore[no-untyped-def]
    [statement] = find_price_statements(text)
    return statement


def test_a_price_cut_with_from_and_to_takes_the_new_price_and_the_direction() -> None:
    s = one(
        "NNPC Retail has reduced the pump price of petrol from ₦1,050 to ₦980 per litre "
        "effective 5 October 2026."
    )
    assert (s.product, s.value, s.direction) == ("pms", Decimal("980"), "down")
    assert s.effective == date(2026, 10, 5)


def test_a_single_price_with_a_rise_verb_is_up() -> None:
    s = one("The ex-depot price of diesel rose to ₦1,200 per litre.")
    assert (s.product, s.value, s.direction, s.depot) == ("ago", Decimal("1200"), "up", True)


def test_a_single_price_without_a_verb_has_unknown_direction() -> None:
    s = one("The price of PMS is now N899/litre at NNPC stations.")
    assert (s.product, s.value, s.direction, s.effective) == (
        "pms",
        Decimal("899"),
        "unknown",
        None,
    )


def test_kerosene_is_recognised() -> None:
    assert one("Household kerosene sells at ₦1,300 per litre.").product == "dpk"


@pytest.mark.parametrize(
    "text",
    [
        "Petrol and diesel will sell at ₦950 per litre.",  # two products, one price
        "Diesel was ₦1,200 per litre, while kerosene was ₦1,300 per litre.",  # two products
        "Diesel rose from ₦900 to ₦1,000 and then ₦1,100 per litre.",  # three amounts
        "NNPC posted a ₦7.2 trillion profit. Petrol supply improved.",  # no price per litre
        "The price of PMS is ₦899.",  # no unit
        "Petrol is ₦0 per litre.",  # implausible
    ],
)
def test_sentences_that_are_ambiguous_or_not_a_price_are_left_alone(text: str) -> None:
    assert find_price_statements(text) == []


def test_lower_case_ago_and_pms_are_ordinary_words_not_products() -> None:
    assert find_price_statements("Two days ago the price was N900 per litre.") == []


def test_a_long_sentence_gives_a_passage_within_the_limit_that_holds_the_price() -> None:
    filler = "the company reviewed many things and the board noted them carefully " * 12
    text = f"Petrol is sold at ₦980 per litre after {filler}today."
    s = one(text)
    assert len(s.passage) <= 300
    assert "₦980 per litre" in s.passage
    assert text[s.start : s.start + len(s.passage)] == s.passage


def test_the_limit_is_honoured_when_the_permission_asks_for_less() -> None:
    [s] = find_price_statements(
        "Petrol is sold at ₦980 per litre at all NNPC retail outlets.", quote_limit=40
    )
    assert len(s.passage) <= 40
    assert "₦980 per litre" in s.passage


def test_each_sentence_is_read_on_its_own() -> None:
    text = "Petrol is ₦980 per litre. Diesel rose to ₦1,200 per litre. NNPC thanks customers."
    assert [(s.product, s.value) for s in find_price_statements(text)] == [
        ("pms", Decimal("980")),
        ("ago", Decimal("1200")),
    ]
