"""The RSS adapter's parsing and keyword filter, on the real feeds saved in ``tests/fixtures/rss``
(no network, no database)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from africasignal.jobs import handlers
from africasignal.models import Source
from africasignal.net.fetch import FetchResult
from africasignal.sources import base
from africasignal.sources.base import AdapterContext
from africasignal.sources.rss import (
    FeedError,
    KeywordMatcher,
    RssAdapter,
    default_matcher,
    parse_feed,
    same_site,
    select_items,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "rss"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
FEEDS = {f["file"]: f for f in MANIFEST["files"]}
HOMES = {
    "punch.xml": "https://punchng.com",
    "premiumtimes.xml": "https://www.premiumtimesng.com",
    "businessday.xml": "https://businessday.ng",
    "channels.xml": "https://www.channelstv.com",
    "vanguard.xml": "https://www.vanguardngr.com",
    "nairametrics.xml": "https://nairametrics.com",
    "dailytrust.xml": "https://dailytrust.com",
    "leadership.xml": "https://leadership.ng",
}


def source(file: str) -> Source:
    return Source(
        slug=file.removesuffix(".xml"),
        name=file,
        home_url=HOMES[file],
        feed_url=FEEDS[file]["feed_url"],
        max_requests_per_hour=60,
    )


def feed_bytes(file: str) -> bytes:
    return (FIXTURES / file).read_bytes()


def xml_feed(*items: str, link: str = "https://example.ng") -> bytes:
    body = "".join(items)
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title><link>{link}</link>'
        f"<description>d</description>{body}</channel></rss>"
    ).encode()


def item(title: str, link: str = "https://example.ng/a", summary: str = "") -> str:
    return (
        f"<item><title>{title}</title><link>{link}</link>"
        f"<description>{summary}</description></item>"
    )


# --- the real feeds ---------------------------------------------------------------------------


@pytest.mark.parametrize("file", sorted(HOMES))
def test_every_saved_feed_parses_to_the_number_of_items_in_its_manifest(file: str) -> None:
    entries = parse_feed(feed_bytes(file), FEEDS[file]["feed_url"])
    assert len(entries) == FEEDS[file]["items"]
    for e in entries:
        assert e["url"].startswith("https://")
        assert e["title"]
        assert e["published_at"] is not None and e["published_at"].tzinfo is not None
        assert "<" not in e["summary"] and "&#" not in e["summary"]


@pytest.mark.parametrize("file", sorted(HOMES))
def test_every_item_is_on_the_outlets_own_site(file: str) -> None:
    entries = parse_feed(feed_bytes(file), FEEDS[file]["feed_url"])
    _, _, off_site = select_items(entries, source(file), default_matcher())
    assert off_site == 0


def test_only_the_items_that_mention_a_topic_are_kept() -> None:
    found: dict[str, list[str]] = {}
    for file in HOMES:
        entries = parse_feed(feed_bytes(file), FEEDS[file]["feed_url"])
        items, not_matching, _ = select_items(entries, source(file), default_matcher())
        assert len(items) + not_matching == len(entries)
        found[file] = [i.title or "" for i in items]
    assert found["punch.xml"] == []
    assert [t[:24] for t in found["nairametrics.xml"]] == [
        "NNPC Group’s tax, royalt",
        "NNPC Group spends N111 b",
        "NNPC’s sundry income hit",
        "FG’s electricity subsidy",
    ]
    assert any("petrol prices" in t for t in found["premiumtimes.xml"])
    assert any("hardship" in t for t in found["vanguard.xml"])
    assert sum(len(v) for v in found.values()) == 6


def test_a_kept_item_carries_its_url_title_and_publication_time() -> None:
    entries = parse_feed(feed_bytes("premiumtimes.xml"), FEEDS["premiumtimes.xml"]["feed_url"])
    items, _, _ = select_items(entries, source("premiumtimes.xml"), default_matcher())
    (kept,) = items
    assert kept.url.startswith("https://www.premiumtimesng.com/")
    assert (
        kept.title
        == "Nigerian workers threaten warning strike over high petrol prices, minimum wage"
    )
    assert kept.published_at is not None and kept.published_at.year == 2026


def test_tracking_parameters_stay_in_the_url_but_the_same_story_is_kept_once() -> None:
    feed = xml_feed(
        item("Petrol price rises", "https://example.ng/a?utm_source=rss"),
        item("Petrol price rises", "https://example.ng/a"),
    )
    items, _, _ = select_items(
        parse_feed(feed, "https://example.ng/feed"), _src(), default_matcher()
    )
    assert len(items) == 1


# --- the keyword filter -----------------------------------------------------------------------


def _src(home: str = "https://example.ng") -> Source:
    return Source(slug="x", name="Example", home_url=home, max_requests_per_hour=60)


MATCHER = default_matcher()


@pytest.mark.parametrize(
    "text",
    [
        "Petrol price hits N1,200 in Lagos",
        "NMDPRA fixes new ex-depot price",
        "DisCos accused of overbilling",
        "Electricity tariff for Band A customers unchanged",
        "Electricity Tariff review ordered",
        "Rice, beans and garri cost more in Kano",
        "Tomatoes now sell for double",
        "Food inflation rose again",
        "NNPC’s refinery returns",
        "Govt to end fuel subsidy",
        "cooking gas shortage deepens",
        "Price of LPG falls",
        "Cost of AGO rises for transporters",
        "Diesel supply tightens",
    ],
)
def test_topic_words_match(text: str) -> None:
    assert MATCHER.match(text), text


@pytest.mark.parametrize(
    "text",
    [
        "He left two days ago",  # "ago" is a word; only capitals AGO is the fuel
        "Stocks fell a week ago",
        "The price of a ticket rose",  # 'rice' inside 'price'
        "A tomatoey sauce",
        "Pmsomething",
        "Nigeria beat Ghana 2-1",
        "Governor commissions a school",
    ],
)
def test_other_text_does_not_match(text: str) -> None:
    assert MATCHER.match(text) == [], text


def test_capitalised_acronyms_do_not_match_ordinary_words() -> None:
    m = KeywordMatcher({"energy": ["PMS", "AGO", "LPG"]})
    assert m.match("two days ago") == []
    assert m.match("AGO price up") == [("energy", "AGO")]
    assert m.match("Pms rates") == []


def test_plurals_and_hyphens_match_but_inner_letters_do_not() -> None:
    m = KeywordMatcher({"food": ["yam", "bread", "rice"]})
    assert m.match("Yams are dearer") == [("food", "yam")]
    assert m.match("bread-and-butter") == [("food", "bread")]
    assert m.match("price of a ticket") == []
    assert m.match("ricey dish") == []


def test_the_summary_counts_as_well_as_the_title() -> None:
    feed = xml_feed(
        item("Market report", "https://example.ng/1", "Traders say garri prices doubled"),
        item("Market report", "https://example.ng/2", "Nothing relevant"),
    )
    items, not_matching, _ = select_items(
        parse_feed(feed, "https://example.ng/feed"), _src(), MATCHER
    )
    assert [i.url for i in items] == ["https://example.ng/1"] and not_matching == 1


def test_markup_and_entities_in_the_summary_are_read_as_text() -> None:
    feed = xml_feed(
        item(
            "Report",
            "https://example.ng/1",
            "&lt;p&gt;The &lt;b&gt;petrol&lt;/b&gt; queue at Ikeja&amp;#8217;s depot&lt;/p&gt;",
        )
    )
    (entry,) = parse_feed(feed, "https://example.ng/feed")
    assert entry["summary"] == "The petrol queue at Ikeja’s depot"


# --- robustness -------------------------------------------------------------------------------


def test_items_on_other_sites_are_skipped_not_followed() -> None:
    feed = xml_feed(
        item("Petrol price", "https://example.ng/a"),
        item("Petrol price again", "https://www.example.ng/b"),
        item("Petrol price elsewhere", "https://evil.test/c"),
        item("Petrol lookalike", "https://example.ng.evil.test/d"),
    )
    items, _, off_site = select_items(parse_feed(feed, "https://example.ng/feed"), _src(), MATCHER)
    assert [i.url for i in items] == ["https://example.ng/a", "https://www.example.ng/b"]
    assert off_site == 2


def test_relative_links_resolve_against_the_feed_and_odd_schemes_are_dropped() -> None:
    feed = xml_feed(
        item("Petrol a", "/story/1"),
        item("Petrol b", "javascript:alert(1)"),
        item("Petrol c", ""),
    )
    entries = parse_feed(feed, "https://example.ng/feed")
    assert [e["url"] for e in entries] == ["https://example.ng/story/1"]


def test_an_empty_feed_is_not_an_error() -> None:
    assert parse_feed(xml_feed(), "https://example.ng/feed") == []


def test_a_challenge_page_is_not_a_feed() -> None:
    page = b"<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    with pytest.raises(FeedError):
        parse_feed(page, "https://example.ng/feed")


@pytest.mark.parametrize(
    ("url", "home", "expected"),
    [
        ("https://punchng.com/a", "https://punchng.com", True),
        ("https://www.punchng.com/a", "https://punchng.com", True),
        ("https://punchng.com/a", "https://www.punchng.com", True),
        ("https://rss.punchng.com/a", "https://punchng.com", True),
        ("https://punchng.com.evil.test/a", "https://punchng.com", False),
        ("https://notpunchng.com/a", "https://punchng.com", False),
        ("https://anything.test/a", None, True),
    ],
)
def test_same_site(url: str, home: str | None, expected: bool) -> None:
    assert same_site(url, home) is expected


# --- the adapter's discover() with a fake fetcher ---------------------------------------------


class FakeFetch:
    def __init__(self, result: FetchResult) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append((url, kwargs))
        return self.result


def ok(content: bytes, url: str = "https://x/feed") -> FetchResult:
    return FetchResult(url=url, status_code=200, content=content)


CTX = AdapterContext(session=None, store=None)  # type: ignore[arg-type]


def test_discover_fetches_only_the_feed_with_the_sources_rate_limit() -> None:
    fetch = FakeFetch(ok(feed_bytes("nairametrics.xml"), "https://nairametrics.com/feed/"))
    items = RssAdapter(fetch=fetch).discover(source("nairametrics.xml"), CTX)
    assert len(items) == 4
    assert len(fetch.calls) == 1
    url, kwargs = fetch.calls[0]
    assert url == "https://nairametrics.com/feed/" and kwargs["max_requests_per_hour"] == 60


def test_a_cloudflare_challenge_is_a_failure_and_is_not_retried_another_way() -> None:
    challenge = FetchResult(
        url="https://www.thecable.ng/feed/",
        status_code=403,
        content=b"<html><title>Just a moment...</title></html>",
    )
    fetch = FakeFetch(challenge)
    src = Source(
        slug="thecable-rss",
        name="TheCable",
        home_url="https://www.thecable.ng",
        feed_url="https://www.thecable.ng/feed/",
        max_requests_per_hour=60,
    )
    with pytest.raises(FeedError, match="403"):
        RssAdapter(fetch=fetch).discover(src, CTX)
    assert len(fetch.calls) == 1


def test_a_source_without_a_feed_url_fails_clearly() -> None:
    src = Source(slug="guardian", name="G", home_url="https://guardian.ng", feed_url=None)
    with pytest.raises(FeedError, match="no feed_url"):
        RssAdapter(fetch=FakeFetch(ok(b""))).discover(src, CTX)


def test_a_200_page_that_is_not_a_feed_fails() -> None:
    fetch = FakeFetch(ok(b"<html><body>Please enable JavaScript</body></html>"))
    with pytest.raises(FeedError):
        RssAdapter(fetch=fetch).discover(source("punch.xml"), CTX)


def test_loading_the_handlers_registers_the_rss_adapter() -> None:
    handlers.load_all()
    assert isinstance(base.get_adapter("rss"), RssAdapter)
