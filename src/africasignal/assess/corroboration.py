"""What the T1 and T2 assessments need to know about claims (spec B8.2, B8.3, AS-027). Pure code.

A ``ClaimPoint`` is one *valid* claim with the facts about its document that evidence rules need:
which reporting origin it came from, what kind of source published it and when. The database
loaders (``publish.situations``, ``publish.policy_situations``) build them; nothing here reads the
database or the clock.

The rules shared by both templates:

* **A trusted source** (``ClaimPoint.trusted``, decided by the loader) is an active source whose
  permission an operator approved. A news outlet must also have been approved for at least
  ``MIN_OUTLET_AGE_DAYS``, so a batch of freshly registered look-alike sites cannot earn a badge.
  Nothing else corroborates or disputes, whatever the claim says (security review S-08).
* **News** is a claim from a trusted ``news_outlet`` source that has a reporting origin. Aggregators
  (GDELT) never count: a domain GDELT merely pointed to is not an outlet anyone vetted. A document
  nobody has clustered into an origin cannot be shown to be independent, so it never counts.
* **Official** is a claim from a trusted ``official_statistics``, ``regulator``, ``government`` or
  ``company`` source (company: a fuel retailer's own price announcement is a primary document).
* **Independence is counted in groups, not documents.** Ten outlets printing the same wire story
  are one origin, and a news document in the same origin as the official measurement it claims to
  confirm is not independent of it. Claims are also joined into one group when their documents
  share a registered domain, the source's owner, or a byline (an author or a wire credit such as
  "Reuters"): one person or company running several sites, or one story under several logos, is
  one voice. ``independent_groups`` does the grouping. A news document that is a near-duplicate of
  the official document itself (``copies_official``, set by the loader from SimHash) is a copy of
  that document, not a second report of it.
* **Only validated text is read.** Keyword and wording checks look at a claim's ``passage``, which
  the extractor proved occurs in the document, never at the model-written ``text`` (S-09).
* **Timing.** A claim is about a window when its dates overlap it. A claim that states no date
  (``unknown`` precision, or no dates at all) falls back to the day its document was published.
  A claim whose dates are only as precise as a year is about no particular month, so it never
  matches.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse

NEWS_SOURCE_KINDS = frozenset({"news_outlet"})  # not "aggregator": see the module docstring
MIN_OUTLET_AGE_DAYS = 30  # an outlet corroborates only after this long under an approved permission
OFFICIAL_SOURCE_KINDS = frozenset({"official_statistics", "regulator", "government", "company"})
DATED_PRECISIONS = frozenset({"day", "month"})
OPPOSITE = {"up": "down", "down": "up"}


@dataclass(frozen=True)
class ClaimPoint:
    claim_id: int
    evidence_document_id: int
    origin_id: int | None
    origin_label: str
    source_name: str
    source_short: str  # for example "NERC"
    source_kind: str  # source.kind
    text: str
    passage: str
    stated_value: Decimal | None
    direction: str  # up | down | unchanged | unknown
    occurred_from: date | None
    occurred_to: date | None
    time_precision: str  # day | month | year | unknown
    published_at: datetime  # when the document was published (or, failing that, retrieved)
    # The document is a near-duplicate of an official document behind the assessment: a copy of
    # the official text, not an independent report of it.
    copies_official: bool = False
    # The source is active with an approved permission (and, for a news outlet, old enough).
    # Fails closed: a claim nobody vetted never counts.
    trusted: bool = False
    # What ties this claim to others: registered domains, owner and byline of its document.
    link_keys: frozenset[str] = frozenset()


def is_news(claim: ClaimPoint) -> bool:
    return claim.trusted and claim.source_kind in NEWS_SOURCE_KINDS and claim.origin_id is not None


def is_official(claim: ClaimPoint) -> bool:
    return claim.trusted and claim.source_kind in OFFICIAL_SOURCE_KINDS


def add_months(d: date, n: int) -> date:
    index = d.year * 12 + (d.month - 1) + n
    return date(index // 12, index % 12 + 1, 1)


def month_end(d: date) -> date:
    return add_months(d, 1) - timedelta(days=1)


def in_window(claim: ClaimPoint, start: date, end: date) -> bool:
    """Whether the claim is about a moment inside ``start``..``end`` (inclusive)."""
    if claim.time_precision == "year":
        return False
    if claim.occurred_from is not None and claim.time_precision in DATED_PRECISIONS:
        first = claim.occurred_from
        last = claim.occurred_to or (month_end(first) if claim.time_precision == "month" else first)
        return first <= end and last >= start
    return start <= claim.published_at.date() <= end


def independent_groups(claims: Iterable[ClaimPoint]) -> list[list[ClaimPoint]]:
    """News claims grouped by who is behind them. The number of groups is the independence count.

    Two claims are in one group when their documents share a reporting origin or any link key
    (registered domain, owner, byline); groups join transitively.
    """
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    news = [c for c in claims if is_news(c)]
    for claim in news:
        nodes = [f"origin:{claim.origin_id}", *sorted(claim.link_keys)]
        for other in nodes[1:]:
            parent[find(other)] = find(nodes[0])
        find(nodes[0])
    groups: dict[str, list[ClaimPoint]] = defaultdict(list)
    for claim in news:
        groups[find(f"origin:{claim.origin_id}")].append(claim)
    return list(groups.values())


_TWO_LEVEL = frozenset({"com", "org", "net", "gov", "edu", "co", "ac", "sch", "mil"})
# Hosts that give every customer a sub-domain: the site is the whole host, so all tenants are one.
_SHARED_HOSTS = frozenset(
    {
        "blogspot.com", "wordpress.com", "medium.com", "substack.com", "github.io", "netlify.app",
        "vercel.app", "pages.dev", "weebly.com", "wixsite.com", "sites.google.com", "tumblr.com",
        "web.app", "firebaseapp.com", "herokuapp.com", "000webhostapp.com",
    }
)  # fmt: skip


def registered_domain(url_or_host: str | None) -> str | None:
    """The registrable domain of an address: ``www.punchng.com`` gives ``punchng.com`` and
    ``news.example.com.ng`` gives ``example.com.ng``. Without the public suffix list this is the
    last two labels (three under ``com.ng`` style suffixes), which errs towards merging sites."""
    if not url_or_host:
        return None
    host = url_or_host if "//" not in url_or_host else urlparse(url_or_host).hostname or ""
    host = host.split("/")[0].split(":")[0].strip(".").lower()
    labels = host.split(".")
    if len(labels) < 2 or not all(labels):
        return host or None
    if ".".join(labels[-2:]) in _SHARED_HOSTS:
        return ".".join(labels[-2:])
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _TWO_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


_CORPORATE = re.compile(r"\b(ltd|limited|plc|inc|llc|nig|nigeria|company|co)\b\.?", re.IGNORECASE)
_GENERIC_BYLINES = frozenset(
    {"staff", "staff reporter", "staff writer", "admin", "administrator", "editor", "newsroom",
     "reporter", "correspondent", "our reporter", "our correspondent", "news desk", "guest",
     "anonymous", "unknown", "editorial"}
)  # fmt: skip


def owner_key(owner: str | None) -> str | None:
    """The owner of a source as a comparable key, or None when none is recorded."""
    if not owner:
        return None
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", _CORPORATE.sub(" ", owner.lower()))
    return " ".join(cleaned.split()) or None


def byline_key(byline: str | None) -> str | None:
    """An author or wire credit as a comparable key. A generic credit ("Staff") names nobody, so
    it links nothing."""
    if not byline:
        return None
    cleaned = re.sub(r"^(by|from)\s+", "", byline.strip().lower())
    cleaned = " ".join(re.sub(r"[^a-z0-9 ]+", " ", cleaned).split())
    return None if not cleaned or cleaned in _GENERIC_BYLINES else cleaned


def link_keys(
    *, urls: Iterable[str | None], owner: str | None, byline: str | None
) -> frozenset[str]:
    """What ties a claim's document to others: its registered domains, owner and byline."""
    keys = {f"domain:{d}" for u in urls if (d := registered_domain(u))}
    if (o := owner_key(owner)) is not None:
        keys.add(f"owner:{o}")
    if (b := byline_key(byline)) is not None:
        keys.add(f"byline:{b}")
    return frozenset(keys)


def matches_any(text: str, keywords: Iterable[str]) -> bool:
    """Whether any keyword occurs as a whole word or phrase, ignoring case."""
    return any(
        re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.IGNORECASE) for word in keywords
    )


def claim_text(claim: ClaimPoint) -> str:
    """The text wording checks read: the passage only, which is proved to be in the document."""
    return claim.passage


def document_ids(claims: Iterable[ClaimPoint]) -> list[int]:
    return sorted({c.evidence_document_id for c in claims})
