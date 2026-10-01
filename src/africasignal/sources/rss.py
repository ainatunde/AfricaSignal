"""RSS/Atom news adapter (spec B6.6, AS-023).

Discovery fetches an outlet's feed and keeps only the items whose title or summary mentions a topic
keyword (``keywords`` in ``config/items.yaml``). Only those are handed to ``process_document``,
which fetches the article; every other item is counted and dropped, so the article page of an
unrelated story is never requested. Processing gives the captured article a reporting origin and
queues claim extraction.

What this adapter deliberately does not do:
* bypass anything: a feed that answers 403 (a Cloudflare challenge, for example) is a failure of
  the source, reported through ``record_failure``, never retried by other means;
* keep article text beyond the source's permission: ``capture`` applies it (news is seeded with
  ``may_store_full_text: false`` and a 300 character quote limit), and this module never writes
  ``text_content`` or ``excerpt`` itself;
* follow a feed to another site: an item whose link is not on the outlet's own site is skipped.
"""

from __future__ import annotations

import calendar
import html
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser

from africasignal.catalog import load_items
from africasignal.evidence.origins import assign_origin
from africasignal.evidence.urls import canonicalise
from africasignal.extract.jobs import enqueue_extraction
from africasignal.models import EvidenceDocument, Source
from africasignal.net.fetch import FetchResult, fetch_document
from africasignal.sources.base import (
    AdapterContext,
    DiscoveredItem,
    ProcessResult,
    register_adapter,
)

log = logging.getLogger("africasignal.sources.rss")

FEED_MAX_BYTES = 5_000_000
# A feed is a window on the newest stories; one far larger than this is not a news feed.
MAX_ITEMS = 200

# An unterminated tag or entity at the end is what a cut-off summary leaves behind.
_TAGS = re.compile(r"<[^>]*(?:>|$)")
_CUT_ENTITY = re.compile(r"&#?\w*$")
_SPACE = re.compile(r"\s+")


class FeedError(Exception):
    """The feed could not be fetched or is not a feed."""


# --- keyword matching -------------------------------------------------------------------------


class KeywordMatcher:
    """Finds topic keywords in a headline and summary.

    A keyword matches a whole word, plus an optional plural ("DisCos", "tomatoes"). Keywords that
    are written entirely in capitals (PMS, AGO, LPG, NERC ...) match only in capitals: "ago" is an
    ordinary word ("two days ago") and must not make a story about something else look like fuel
    news. Everything else ignores case.
    """

    def __init__(self, keywords: dict[str, list[str]]) -> None:
        self._patterns: list[tuple[str, str, re.Pattern[str]]] = []
        for topic, words in keywords.items():
            for word in words:
                flags = 0 if word.isupper() else re.IGNORECASE
                pattern = re.compile(rf"(?<![\w]){re.escape(word)}(?:e?s)?(?![\w])", flags)
                self._patterns.append((topic, word, pattern))

    def match(self, *texts: str | None) -> list[tuple[str, str]]:
        """``(topic, keyword)`` pairs found in ``texts``, each keyword at most once."""
        haystack = "\n".join(t for t in texts if t)
        return [(topic, word) for topic, word, p in self._patterns if p.search(haystack)]


@lru_cache(maxsize=1)
def default_matcher() -> KeywordMatcher:
    return KeywordMatcher({str(t): list(w) for t, w in load_items().keywords.items()})


# --- feed parsing -----------------------------------------------------------------------------


def _plain(markup: str | None) -> str:
    """Text of a feed field: tags dropped, entities decoded, whitespace collapsed."""
    if not markup:
        return ""
    text = _CUT_ENTITY.sub("", _TAGS.sub(" ", markup))
    return _SPACE.sub(" ", html.unescape(text)).strip()


def _entry_time(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
    return None


def _site(host: str) -> str:
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, home_url: str | None) -> bool:
    """True when ``url`` is on the outlet's own site (its home page host or a subdomain of it)."""
    if not home_url:
        return True
    host, home = _site(urlparse(url).hostname or ""), _site(urlparse(home_url).hostname or "")
    return bool(host) and (host == home or host.endswith("." + home))


def parse_feed(content: bytes, feed_url: str) -> list[dict[str, Any]]:
    """The entries of a feed as ``{url, title, summary, published_at}`` dicts, in feed order.

    Raises ``FeedError`` when the bytes are not a feed (an HTML challenge page, for example).
    Entries without a usable link are dropped.
    """
    parsed = feedparser.parse(content)
    if not parsed.version:  # feedparser found no RSS or Atom in it
        raise FeedError("the response is not an RSS or Atom feed")
    entries: list[dict[str, Any]] = []
    for entry in parsed.entries[:MAX_ITEMS]:
        link = (entry.get("link") or "").strip()
        if not link:
            continue
        url = urljoin(feed_url, link)
        if urlparse(url).scheme not in ("http", "https"):
            continue
        entries.append(
            {
                "url": url,
                "title": _plain(entry.get("title")) or None,
                "summary": _plain(entry.get("summary")),
                "published_at": _entry_time(entry),
                "byline": _plain(entry.get("author")) or None,
            }
        )
    return entries


def select_items(
    entries: Iterable[dict[str, Any]],
    source: Source,
    matcher: KeywordMatcher,
) -> tuple[list[DiscoveredItem], int, int]:
    """Split ``entries`` into the items worth fetching and counts of the rest.

    Returns ``(items, not_matching, off_site)``. The items hold one entry per canonical URL.
    """
    items: list[DiscoveredItem] = []
    seen: set[str] = set()
    not_matching = off_site = 0
    for entry in entries:
        url = entry["url"]
        if not same_site(url, source.home_url):
            off_site += 1
            continue
        key = canonicalise(url)
        if key in seen:
            continue
        seen.add(key)
        if not matcher.match(entry["title"], entry["summary"]):
            not_matching += 1
            continue
        items.append(
            DiscoveredItem(
                url=url,
                title=entry["title"],
                published_at=entry["published_at"],
                byline=entry.get("byline"),
            )
        )
    return items, not_matching, off_site


# --- adapter ----------------------------------------------------------------------------------


class RssAdapter:
    slug_prefix = "rss"

    def __init__(
        self,
        fetch: Any = fetch_document,
        matcher: KeywordMatcher | None = None,
    ) -> None:
        self._fetch = fetch
        self._matcher = matcher

    def discover(self, source: Source, ctx: AdapterContext) -> list[DiscoveredItem]:
        if not source.feed_url:
            raise FeedError(f"source {source.slug} has no feed_url")
        result: FetchResult = self._fetch(
            source.feed_url,
            max_requests_per_hour=source.max_requests_per_hour,
            max_bytes=FEED_MAX_BYTES,
        )
        if not result.success:
            raise FeedError(
                result.error or f"fetch of {source.feed_url} returned HTTP {result.status_code}"
            )
        entries = parse_feed(result.content, result.url or source.feed_url)
        items, not_matching, off_site = select_items(
            entries, source, self._matcher or default_matcher()
        )
        log.info(
            "feed %s: %d items, %d to fetch, %d not about our topics, %d off-site",
            source.slug,
            len(entries),
            len(items),
            not_matching,
            off_site,
        )
        return items

    def process(self, doc: EvidenceDocument, ctx: AdapterContext) -> ProcessResult:
        origin = assign_origin(ctx.session, doc)  # already set by process_document; idempotent
        result = ProcessResult()
        if enqueue_extraction(ctx.session, doc.id) is not None:
            result.notes.append(f"queued claim extraction for document {doc.id}")
        result.notes.append(f"reporting origin {origin.id} ({origin.kind})")
        return result


register_adapter("rss", RssAdapter())
