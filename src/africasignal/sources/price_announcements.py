"""Fuel price announcements from company and regulator press pages (spec B6.5, AS-026).

Adapter name ``price_announcement``. Only NNPC Limited can be read today:

* **Discovery (NNPC).** nnpcgroup.com/insights is filled in by the browser from a Strapi CMS API
  (``feed_url`` in ``sources.yaml``). The newest posts are read from that JSON; a post is listed
  only if its text talks about a fuel price, because most posts are corporate results and project
  news. The JSON is used to decide what to fetch and is not stored. Each listed post is fetched as
  its own server-rendered page (``<home_url>/insights/<slug>``) through ``process_document``.
* **NMDPRA** is a JavaScript-only site with nothing to read statically: its source stays inactive
  and ``discover`` refuses it by name until a route is found.
* **Processing (any HTML or PDF with text).** Sentences that give one fuel product (petrol/PMS,
  diesel/AGO, kerosene/DPK) a naira amount per litre become claims, with the amount parsed in code.
  Petrol becomes a ``policy_statement`` for the series ``pms_regulated_price``; diesel and kerosene
  become ``price_statement`` claims for ``ago_litre`` and ``dpk_litre``. A sentence that names two
  products, or several amounts without a "from ... to ..." shape, is left alone: a wrong price is
  worse than a missing one.

The passage stored with a claim never exceeds the source permission's ``max_quote_chars``.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.evidence.text import extract_text
from africasignal.models import Claim, EvidenceDocument, Place, ReportingOrigin, Source
from africasignal.net.fetch import FetchResult, fetch_document
from africasignal.sources.base import (
    AdapterContext,
    DiscoveredItem,
    ProcessResult,
    register_adapter,
)
from africasignal.sources.permissions import current_permission

log = logging.getLogger("africasignal.sources.price_announcements")

EXTRACTOR_VERSION = "price_announcement_v1"
PRICE_UNIT = "NGN/litre"
FEED_QUERY = "sort=publishedAt:desc&pagination[pageSize]=25"
FEED_MAX_BYTES = 5_000_000  # ten posts were 80 KB when this was written
DEFAULT_QUOTE_CHARS = 300
NNPC_HOST = "nnpcgroup.com"
NATIONAL_CODE = "NG"

# product -> (item code, policy series or None). Petrol has a policy series; the others are plain
# price statements.
PRODUCTS: dict[str, tuple[str | None, str | None]] = {
    "pms": ("pms_litre", "pms_regulated_price"),
    "ago": ("ago_litre", None),
    "dpk": ("dpk_litre", None),
}
_PRODUCT_WORDS = {
    "pms": re.compile(r"\b(?:petrol|premium\s+motor\s+spirit)\b|\bPMS\b", re.IGNORECASE),
    "ago": re.compile(r"\bdiesel\b|\bautomotive\s+gas\s+oil\b|\bAGO\b", re.IGNORECASE),
    "dpk": re.compile(r"\bkerosene\b|\bDPK\b", re.IGNORECASE),
}
# "PMS" and "AGO" match only in capitals ("ago" is a common word): handled by the flag split above,
# but the word list patterns are case-insensitive, so the acronyms are re-checked below.
_ACRONYM_ONLY = {"pms": "PMS", "ago": "AGO", "dpk": "DPK"}
_AMOUNT = re.compile(
    r"(?<![A-Za-z])(?:₦|NGN\s?|N)\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s*(?:/|per\s+|a\s+|each\s+)\s?(?:litres?|liters?|ltrs?|l)\b)?",
    re.IGNORECASE,
)
_PER_LITRE = re.compile(r"(?:/|\bper\s+|\ba\s+|\beach\s+)\s?(?:litres?|liters?|ltrs?|l)\b", re.I)
_FROM_TO = re.compile(r"\bfrom\b.*?\bto\b", re.IGNORECASE | re.DOTALL)
_UP = re.compile(r"\b(?:increas\w+|rais\w+|hik\w+|rise[sn]?|rose|up)\b", re.IGNORECASE)
_DOWN = re.compile(r"\b(?:reduc\w+|cut\w*|lower\w*|slash\w*|drop\w*|fall\w*|fell|down)\b", re.I)
_DEPOT = re.compile(r"\b(?:ex-?\s?depot|ex-?\s?refinery|gantry|dealers?)\b", re.IGNORECASE)
_EFFECTIVE = re.compile(
    r"\b(?:from|effective|with effect from|starting|beginning)\s+(?:on\s+)?(?:the\s+)?"
    r"(?P<day>\d{1,2})\S{0,3}\s+(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December),?\s+(?P<year>\d{4})",
    re.IGNORECASE,
)
_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]  # fmt: skip
_SENTENCE_BREAK = re.compile(r"(?<=[.;!?])\s+(?=[A-Z₦\"“])|\n\s*\n|\n")
_TAGS = re.compile(r"<[^>]+>")

Fetcher = Callable[..., FetchResult]


class AnnouncementError(Exception):
    """A price announcement source could not be read."""


class UnsupportedSource(AnnouncementError):
    """No way of reading this source has been built."""


# --------------------------------------------------------------------------------------------
# Reading prices out of text
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceStatement:
    product: str  # pms | ago | dpk
    value: Decimal
    direction: str
    passage: str  # exact text, at most the quote limit
    start: int
    effective: date | None
    depot: bool  # an ex-depot, gantry or dealer price, not a pump price


def _products_in(sentence: str) -> set[str]:
    found = set()
    for product, pattern in _PRODUCT_WORDS.items():
        for m in pattern.finditer(sentence):
            word = m.group(0)
            if word.upper() == _ACRONYM_ONLY[product] and word != _ACRONYM_ONLY[product]:
                continue  # "ago"/"pms" in lower case is an ordinary word
            found.add(product)
    return found


def _bounded(text: str, start: int, end: int, anchor: int, limit: int) -> tuple[str, int]:
    """The passage for the sentence ``text[start:end]``: all of it if it fits in ``limit``
    characters, else the stretch around ``anchor`` cut at word boundaries. Returns the passage and
    its offset."""
    if end - start <= limit:
        return text[start:end].strip(), start + (
            len(text[start:end]) - len(text[start:end].lstrip())
        )
    lo = max(start, min(anchor - limit // 2, end - limit))
    hi = min(end, lo + limit)
    if lo > start and not text[lo - 1].isspace():
        nxt = re.search(r"\s", text[lo:hi])
        lo += nxt.end() if nxt else 0
    if hi < end and not text[hi].isspace():
        cut = max(text.rfind(" ", lo, hi), text.rfind("\n", lo, hi))
        hi = cut if cut > lo else hi
    return text[lo:hi].strip(), lo + (len(text[lo:hi]) - len(text[lo:hi].lstrip()))


def find_price_statements(
    text: str, *, quote_limit: int = DEFAULT_QUOTE_CHARS
) -> list[PriceStatement]:
    """Fuel price statements in ``text``. See the module docstring for what is and is not read."""
    found: list[PriceStatement] = []
    position = 0
    for piece in _SENTENCE_BREAK.split(text):
        start = text.find(piece, position)
        if start < 0 or not piece.strip():
            continue
        position = start + len(piece)
        products = _products_in(piece)
        if len(products) != 1:
            continue
        every = list(_AMOUNT.finditer(piece))
        if len(every) == 2 and _FROM_TO.search(piece) and _PER_LITRE.search(piece):
            amounts = every  # "from ₦900 to ₦1,050 per litre": the unit follows the last amount
        elif len(every) == 1:
            amounts = [m for m in every if _PER_LITRE.search(m.group(0))]
        else:
            continue  # several amounts that are not one "from ... to ...": too ambiguous to read
        if not amounts:
            continue
        values = [Decimal(m["num"].replace(",", "")) for m in amounts]
        direction = "unknown"
        if len(values) == 2 and _FROM_TO.search(piece):
            before, value = values
            direction = "up" if value > before else "down" if value < before else "unchanged"
            anchor = amounts[1].start()
        elif len(values) == 1:
            value, anchor = values[0], amounts[0].start()
            up, down = bool(_UP.search(piece)), bool(_DOWN.search(piece))
            direction = "up" if up and not down else "down" if down and not up else "unknown"
        else:
            continue
        if not Decimal(1) <= value <= Decimal(100_000):
            continue
        passage, offset = _bounded(text, start, start + len(piece), start + anchor, quote_limit)
        eff = _EFFECTIVE.search(piece)
        effective = None
        if eff:
            try:
                effective = date(
                    int(eff["year"]), _MONTHS.index(eff["month"].lower()) + 1, int(eff["day"])
                )
            except ValueError:
                effective = None
        found.append(
            PriceStatement(
                product=next(iter(products)),
                value=value,
                direction=direction,
                passage=passage,
                start=offset,
                effective=effective,
                depot=bool(_DEPOT.search(piece)),
            )
        )
    return found


def mentions_fuel_price(text: str) -> bool:
    """Whether text is about a fuel price at all: a fuel product and the word price (or a naira
    amount per litre). Used to choose which listed posts are worth fetching."""
    has_product = any(_products_in(text))
    return has_product and (
        bool(re.search(r"\bprices?\b|\bpump\s+price\b", text, re.IGNORECASE))
        or bool(find_price_statements(text))
    )


# --------------------------------------------------------------------------------------------
# NNPC's post feed
# --------------------------------------------------------------------------------------------


def parse_nnpc_posts(payload: bytes, home_url: str) -> list[tuple[DiscoveredItem, str]]:
    """The posts in a Strapi ``/api/posts`` response, each with its plain text (for filtering
    only). The page URL of a post is ``<home_url>/insights/<slug>``."""
    try:
        data = json.loads(payload)["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise AnnouncementError(f"the NNPC feed is not the expected JSON: {exc}") from exc
    if not isinstance(data, list):
        raise AnnouncementError("the NNPC feed has no post list")
    base = home_url.rstrip("/")
    posts = []
    for post in data:
        slug, title = post.get("slug"), post.get("title")
        if not slug or not title:
            continue
        stamp = post.get("publishedAt") or post.get("post_date")
        published = None
        if stamp:
            try:
                published = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
            except ValueError:
                published = None
        plain = html.unescape(_TAGS.sub(" ", post.get("content") or ""))
        posts.append(
            (
                DiscoveredItem(
                    url=f"{base}/insights/{slug}", title=str(title), published_at=published
                ),
                f"{title}\n{plain}",
            )
        )
    return posts


def _origin(session: Session, document: EvidenceDocument, label: str) -> None:
    if document.origin_id is not None:
        return
    origin = ReportingOrigin(
        kind="primary_document", label=label, first_seen_at=document.retrieved_at
    )
    session.add(origin)
    session.flush()
    document.origin_id = origin.id


class PriceAnnouncementAdapter:
    slug_prefix = "price_announcement"

    def __init__(self, fetch: Fetcher = fetch_document) -> None:
        self._fetch = fetch

    def discover(self, source: Source, ctx: AdapterContext) -> list[DiscoveredItem]:
        host = urlparse(source.home_url or "").hostname or ""
        if not (host == NNPC_HOST or host.endswith("." + NNPC_HOST)):
            raise UnsupportedSource(
                f"{source.slug}: no way of reading {host or 'this source'} has been built"
            )
        if not source.feed_url:
            raise AnnouncementError(f"source {source.slug} has no feed_url")
        assert source.home_url
        joiner = "&" if "?" in source.feed_url else "?"
        url = f"{source.feed_url}{joiner}{FEED_QUERY}"
        result = self._fetch(
            url, max_requests_per_hour=source.max_requests_per_hour, max_bytes=FEED_MAX_BYTES
        )
        if not result.success:
            raise AnnouncementError(
                result.error or f"fetch of {url} returned HTTP {result.status_code}"
            )
        posts = parse_nnpc_posts(result.content, source.home_url)
        known = set(
            ctx.session.scalars(
                select(EvidenceDocument.url).where(EvidenceDocument.source_id == source.id)
            )
        )
        return [
            item for item, plain in posts if item.url not in known and mentions_fuel_price(plain)
        ]

    def process(self, doc: EvidenceDocument, ctx: AdapterContext) -> ProcessResult:
        result = ProcessResult()
        source = ctx.session.get(Source, doc.source_id)
        if source is None:
            raise AnnouncementError(f"document {doc.id} has no source")
        _origin(ctx.session, doc, source.name)
        already = ctx.session.scalar(
            select(Claim.id)
            .where(
                Claim.evidence_document_id == doc.id, Claim.extractor_version == EXTRACTOR_VERSION
            )
            .limit(1)
        )
        if already is not None:
            result.notes.append("price claims already stored for this document")
            return result

        # The source's permission may forbid keeping the text, so read it from the raw file each
        # time and keep nothing.
        text = doc.text_content or extract_text(ctx.store.get(doc.storage_key), doc.mime).text
        if not text:
            result.notes.append(f"no readable text in the document ({doc.mime})")
            return result
        permission = current_permission(ctx.session, source.id)
        limit = DEFAULT_QUOTE_CHARS
        if permission is not None and permission.max_quote_chars is not None:
            limit = permission.max_quote_chars
        statements = find_price_statements(text, quote_limit=limit) if limit > 0 else []
        if not statements:
            result.notes.append("no fuel price statement found")
            return result

        country = ctx.session.scalars(
            select(Place.id).where(Place.code == NATIONAL_CODE)
        ).one_or_none()
        stamp = doc.published_at.date() if doc.published_at else None
        claims = []
        for s in statements:
            item_code, series = PRODUCTS[s.product]
            when = s.effective or stamp
            what = {"pms": "petrol (PMS)", "ago": "diesel (AGO)", "dpk": "kerosene (DPK)"}[
                s.product
            ]
            qualifier = " ex-depot" if s.depot else ""
            claims.append(
                Claim(
                    evidence_document_id=doc.id,
                    claim_type="policy_statement" if series else "price_statement",
                    text=f"{source.name} states a{qualifier} {what} price of ₦{s.value} per litre",
                    passage=s.passage,
                    passage_start=s.start,
                    passage_end=s.start + len(s.passage),
                    item_code=None if series else item_code,
                    policy_series=series,
                    stated_value=s.value,
                    stated_unit=PRICE_UNIT,
                    direction=s.direction,
                    occurred_from=when,
                    occurred_to=None,
                    time_precision="day" if when else "unknown",
                    place_candidates=[],
                    place_id=None if series else country,
                    place_precision="unknown" if series or country is None else "national",
                    extractor_version=EXTRACTOR_VERSION,
                    valid=True,
                )
            )
        ctx.session.add_all(claims)
        ctx.session.flush()
        result.claims = len(claims)
        return result


register_adapter("price_announcement", PriceAnnouncementAdapter())
