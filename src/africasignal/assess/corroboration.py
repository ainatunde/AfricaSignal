"""What the T1 and T2 assessments need to know about claims (spec B8.2, B8.3, AS-027). Pure code.

A ``ClaimPoint`` is one *valid* claim with the facts about its document that evidence rules need:
which reporting origin it came from, what kind of source published it and when. The database
loaders (``publish.situations``, ``publish.policy_situations``) build them; nothing here reads the
database or the clock.

The rules shared by both templates:

* **News** is a claim from a ``news_outlet`` or ``aggregator`` source that has a reporting origin.
  A document nobody has clustered into an origin cannot be shown to be independent, so it never
  counts.
* **Official** is a claim from an ``official_statistics``, ``regulator``, ``government`` or
  ``company`` source (company: a fuel retailer's own price announcement is a primary document).
* **Independence is counted in origins, not documents.** Ten outlets printing the same wire story
  are one origin, and a news document in the same origin as the official measurement it claims to
  confirm is not independent of it. A news document that is a near-duplicate of the official
  document itself (``copies_official``, set by the loader from SimHash) is a copy of that document,
  not a second report of it. ``independent_origins`` does the grouping.
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

NEWS_SOURCE_KINDS = frozenset({"news_outlet", "aggregator"})
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


def is_news(claim: ClaimPoint) -> bool:
    return claim.source_kind in NEWS_SOURCE_KINDS and claim.origin_id is not None


def is_official(claim: ClaimPoint) -> bool:
    return claim.source_kind in OFFICIAL_SOURCE_KINDS


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


def independent_origins(claims: Iterable[ClaimPoint]) -> dict[int, list[ClaimPoint]]:
    """News claims grouped by reporting origin. The number of keys is the independence count."""
    groups: dict[int, list[ClaimPoint]] = defaultdict(list)
    for claim in claims:
        if is_news(claim):
            assert claim.origin_id is not None
            groups[claim.origin_id].append(claim)
    return dict(groups)


def matches_any(text: str, keywords: Iterable[str]) -> bool:
    """Whether any keyword occurs as a whole word or phrase, ignoring case."""
    return any(
        re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.IGNORECASE) for word in keywords
    )


def claim_text(claim: ClaimPoint) -> str:
    return f"{claim.text} {claim.passage}"


def document_ids(claims: Iterable[ClaimPoint]) -> list[int]:
    return sorted({c.evidence_document_id for c in claims})
