"""Code-side checks on the claims a model extracted (spec B7).

The model's answer is never trusted. Every claim is checked against the document text, and a
claim that fails is kept with ``valid=False`` and the first reason (for the evaluation set) but
never used in an assessment.

Checks, in this order (the first failure is the stored reason):
1. ``passage_not_found``: the passage is not in the document, ignoring differences in whitespace.
2. ``value_not_in_passage``: ``stated_value`` does not appear in the passage. Thousands
   separators, a leading N or ₦, and the scale words thousand, million, billion and trillion are
   understood ("N1.2 million" contains 1200000).
3. ``invalid_date``: a date that is not ``YYYY-MM-DD``, or ``occurred_to`` before ``occurred_from``.
4. ``future_date``: a date after the document's publication date plus one day, for a passage that
   is not about the future. The model's schema has no tense, so a passage counts as "about the
   future" when it has a future marker such as "will", "is expected to" or "from next month";
   those may carry a future date (a tariff taking effect next month).
5. ``unknown_item_code`` / ``unknown_policy_series``: a code that is not in the allowed lists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

PASSAGE_NOT_FOUND = "passage_not_found"
VALUE_NOT_IN_PASSAGE = "value_not_in_passage"
INVALID_DATE = "invalid_date"
FUTURE_DATE = "future_date"
UNKNOWN_ITEM_CODE = "unknown_item_code"
UNKNOWN_POLICY_SERIES = "unknown_policy_series"

_SCALES = {
    "thousand": Decimal(10) ** 3,
    "million": Decimal(10) ** 6,
    "billion": Decimal(10) ** 9,
    "trillion": Decimal(10) ** 12,
}
# 1,234,567.89 | 1234.5 | 12, optionally followed by a scale word. Digits glued to a digit,
# comma or point before them are not a number start ("1,2003" is not read as 2003).
_NUMBER = re.compile(
    r"(?<![\d.,])(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"(?:\s*(?P<scale>thousand|million|billion|trillion)\b)?",
    re.IGNORECASE,
)
_FUTURE = re.compile(
    r"\b(will|shall|would|is expected to|are expected to|expected to|plans? to|planning to|"
    r"set to|scheduled|to take effect|takes? effect|from next|next (?:week|month|year)|"
    r"effective (?:from|on))\b",
    re.IGNORECASE,
)


def normalise_whitespace(text: str) -> str:
    return " ".join(text.split())


def find_passage(document_text: str, passage: str) -> tuple[int, int] | None:
    """Where ``passage`` occurs in the document, as ``(start, end)`` offsets into the original
    text, matching any run of whitespace in the passage against any run in the document."""
    tokens = passage.split()
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    match = re.search(pattern, document_text)
    return (match.start(), match.end()) if match else None


def numbers_in(text: str) -> set[Decimal]:
    """Every number the text contains, with thousands separators removed and scale words applied."""
    found: set[Decimal] = set()
    for m in _NUMBER.finditer(text):
        value = Decimal(m["int"].replace(",", "") + (m["frac"] or ""))
        found.add(value)
        if m["scale"]:
            found.add(value * _SCALES[m["scale"].lower()])
    return found


def number_in_passage(value: Decimal, passage: str) -> bool:
    return value in numbers_in(passage)


def _parse_date(raw: str | None) -> date | None:
    if raw is None:
        return None
    return date.fromisoformat(raw)


@dataclass(frozen=True)
class ValidatedClaim:
    """A claim ready to store, with its verdict. Field names follow the ``claim`` table."""

    claim_type: str
    text: str
    passage: str
    passage_start: int | None
    passage_end: int | None
    item_code: str | None
    policy_series: str | None
    stated_value: Decimal | None
    stated_unit: str | None
    direction: str
    occurred_from: date | None
    occurred_to: date | None
    time_precision: str
    place_candidates: list[str] = field(default_factory=list)
    valid: bool = True
    invalid_reason: str | None = None


def _stated_value(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:  # the schema promises a number; be safe if a provider slips
        return Decimal("NaN")


def validate_claim(
    raw: dict[str, Any],
    document_text: str,
    published_at: datetime | None,
    allowed_item_codes: set[str],
    allowed_policy_series: set[str],
) -> ValidatedClaim:
    passage = str(raw["passage"]).strip()
    span = find_passage(document_text, passage)
    value = _stated_value(raw.get("stated_value"))

    reason: str | None = None
    occurred_from = occurred_to = None
    try:
        occurred_from = _parse_date(raw.get("occurred_from"))
        occurred_to = _parse_date(raw.get("occurred_to"))
        dates_ok = occurred_from is None or occurred_to is None or occurred_to >= occurred_from
    except ValueError:
        occurred_from = occurred_to = None
        dates_ok = False

    if span is None:
        reason = PASSAGE_NOT_FOUND
    elif value is not None and not number_in_passage(value, passage):
        reason = VALUE_NOT_IN_PASSAGE
    elif not dates_ok:
        reason = INVALID_DATE
    elif _is_future(occurred_from, occurred_to, published_at, passage):
        reason = FUTURE_DATE
    elif raw.get("item_code") is not None and raw["item_code"] not in allowed_item_codes:
        reason = UNKNOWN_ITEM_CODE
    elif raw.get("policy_series") is not None and raw["policy_series"] not in allowed_policy_series:
        reason = UNKNOWN_POLICY_SERIES

    return ValidatedClaim(
        claim_type=raw["claim_type"],
        text=raw["text"],
        passage=passage,
        passage_start=span[0] if span else None,
        passage_end=span[1] if span else None,
        item_code=raw.get("item_code"),
        policy_series=raw.get("policy_series"),
        stated_value=value if value is not None and value.is_finite() else None,
        stated_unit=raw.get("stated_unit"),
        direction=raw["direction"],
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        time_precision=raw["time_precision"],
        place_candidates=[str(p) for p in raw.get("place_candidates", [])],
        valid=reason is None,
        invalid_reason=reason,
    )


def _is_future(
    occurred_from: date | None,
    occurred_to: date | None,
    published_at: datetime | None,
    passage: str,
) -> bool:
    if published_at is None or _FUTURE.search(passage):
        return False
    limit = published_at.date() + timedelta(days=1)
    return any(d is not None and d > limit for d in (occurred_from, occurred_to))


def validate_claims(
    raws: list[dict[str, Any]],
    document_text: str,
    published_at: datetime | None,
    allowed_item_codes: set[str],
    allowed_policy_series: set[str],
) -> list[ValidatedClaim]:
    return [
        validate_claim(r, document_text, published_at, allowed_item_codes, allowed_policy_series)
        for r in raws
    ]
