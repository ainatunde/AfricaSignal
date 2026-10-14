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
6. ``invalid_field``: ``claim_type``, ``direction`` or ``time_precision`` is not one of the allowed
   words (S-09). The claim is kept as invalid with that field set to its neutral value, so a
   model that slips a new word in cannot fail the whole document at the database.
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
INVALID_FIELD = "invalid_field"
UNIT_MISMATCH = "unit_mismatch"
UNIT_NOT_IN_PASSAGE = "unit_not_in_passage"
DIRECTION_MISMATCH = "direction_mismatch"

CLAIM_TYPES = ("price_statement", "policy_statement", "other")
DIRECTIONS = ("up", "down", "unchanged", "unknown")
TIME_PRECISIONS = ("day", "month", "year", "unknown")

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
_DIRECTION_CUES = {
    "up": re.compile(
        r"\b(?:rose|rise[ns]?|rising|increas(?:e|ed|es|ing)|went up|up from|up by|"
        r"higher|surged?|jumped?|climbed?|gained?)\b",
        re.IGNORECASE,
    ),
    "down": re.compile(
        r"\b(?:fell|fall(?:s|ing)?|decreas(?:e|ed|es|ing)|dropped?|declin(?:e|ed|es|ing)|"
        r"went down|down from|down by|lower|reduced?|cut)\b",
        re.IGNORECASE,
    ),
    "unchanged": re.compile(
        r"\b(?:unchanged|remained the same|stayed the same|no change|no increase|no decrease)\b",
        re.IGNORECASE,
    ),
}
_NEGATED_CUE = re.compile(
    r"\b(?:not|never|didn't|doesn't|don't|hasn't|haven't|hadn't|did not|does not|"
    r"do not|has not|have not|had not|no longer|cannot|can't|without)"
    r"(?:\s+\w+){0,2}\s*$",
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


def _unit_dimension(unit: str) -> str | None:
    """Map the units used in the item and policy catalogues to comparable dimensions."""
    value = unit.casefold().replace(",", ".")
    if re.search(r"(?<!\d)12\.5\s*(?:kg|kilograms?)\b", value):
        return "12.5kg"
    if re.search(r"(?<!\d)5\s*(?:kg|kilograms?)\b", value):
        return "5kg"
    if re.search(r"\b(?:kwh|kilowatt[- ]?hours?)\b", value):
        return "kwh"
    if re.search(r"%|\bpercent(?:age)?\b", value):
        return "percent"
    if re.search(r"\b(?:kg|kilograms?)\b", value):
        return "kg"
    if re.search(r"\b(?:litres?|liters?)\b|\bper\s+l\b", value):
        return "litre"
    if re.search(r"\b(?:tonnes?|tons?)\b", value):
        return "tonne"
    return None


def _unit_currency(unit: str) -> str | None:
    prefix = re.split(r"[/\s]", unit.strip(), maxsplit=1)[0].upper()
    if prefix in {"NGN", "USD", "GHS", "KES", "EUR", "GBP"}:
        return prefix
    if "₦" in unit:
        return "NGN"
    if "$" in unit:
        return "USD"
    return None


def _unit_appears_with_value(value: Decimal, unit: str, passage: str) -> bool:
    """Require the selected quantity and its unit to occur close together in the quote."""
    dimension = _unit_dimension(unit)
    if dimension is None:
        return False
    for match in _NUMBER.finditer(passage):
        raw_value = Decimal(match["int"].replace(",", "") + (match["frac"] or ""))
        possibilities = {raw_value}
        if match["scale"]:
            possibilities.add(raw_value * _SCALES[match["scale"].lower()])
        if value not in possibilities:
            continue
        nearby = passage[max(0, match.start() - 20) : min(len(passage), match.end() + 45)]
        if _unit_dimension(nearby) == dimension:
            return True
    return False


def _passage_currency_conflicts(
    value: Decimal, expected_currency: str | None, passage: str
) -> bool:
    if expected_currency is None:
        return False
    for match in _NUMBER.finditer(passage):
        raw_value = Decimal(match["int"].replace(",", "") + (match["frac"] or ""))
        possibilities = {raw_value}
        if match["scale"]:
            possibilities.add(raw_value * _SCALES[match["scale"].lower()])
        if value not in possibilities:
            continue
        nearby = passage[max(0, match.start() - 12) : match.end()]
        has_usd = bool(re.search(r"\$|\bUSD\b|\b(?:US\s+)?dollars?\b", nearby, re.IGNORECASE))
        has_ngn = bool(re.search(r"₦|\bNGN\b|(?<!\w)N(?=\s*[\d,.])", nearby, re.IGNORECASE))
        if expected_currency == "NGN" and has_usd and not has_ngn:
            return True
        if expected_currency == "USD" and has_ngn and not has_usd:
            return True
    return False


def _value_has_currency(value: Decimal, expected_currency: str | None, passage: str) -> bool:
    """Require currency to bind to the selected number, not merely another number nearby."""
    prefixes = {
        "NGN": r"(?:₦|\bNGN\b|(?<!\w)N)\s*$",
        "USD": r"(?:\$|\bUSD\b|\bUS\$)\s*$",
        "GHS": r"(?:₵|\bGHS\b)\s*$",
        "KES": r"(?:\bKES\b)\s*$",
        "EUR": r"(?:€|\bEUR\b)\s*$",
        "GBP": r"(?:£|\bGBP\b)\s*$",
    }
    suffixes = {
        "NGN": r"\s*(?:naira|NGN)\b",
        "USD": r"\s*(?:US\s+)?dollars?\b",
        "GHS": r"\s*(?:Ghanaian\s+)?cedis?\b",
        "KES": r"\s*shillings?\b",
        "EUR": r"\s*euros?\b",
        "GBP": r"\s*pounds?\b",
    }
    pattern = prefixes.get(expected_currency or "")
    suffix_pattern = suffixes.get(expected_currency or "")
    if pattern is None or suffix_pattern is None:
        return True
    for match in _NUMBER.finditer(passage):
        raw_value = Decimal(match["int"].replace(",", "") + (match["frac"] or ""))
        possibilities = {raw_value}
        if match["scale"]:
            possibilities.add(raw_value * _SCALES[match["scale"].lower()])
        if value not in possibilities:
            continue
        prefix = passage[max(0, match.start() - 12) : match.start()]
        suffix = passage[match.end() : min(len(passage), match.end() + 12)]
        if re.search(pattern, prefix, re.IGNORECASE) or re.match(
            suffix_pattern, suffix, re.IGNORECASE
        ):
            return True
    return False


def _explicit_direction(passage: str) -> str | None:
    """Return one clearly stated direction; ambiguity is represented as unknown."""
    found: set[str] = set()
    for direction, pattern in _DIRECTION_CUES.items():
        for match in pattern.finditer(passage):
            prefix = passage[max(0, match.start() - 45) : match.start()]
            if _NEGATED_CUE.search(prefix):
                continue
            found.add(direction)
    # Numeric comparisons can contradict a nearby verb ("rose from N1,200 to N1,000").
    # Treat that passage as ambiguous rather than letting the verb alone set a direction.
    matches = list(_NUMBER.finditer(passage))
    for first, second in zip(matches, matches[1:], strict=False):
        before = passage[max(0, first.start() - 30) : first.start()]
        between = passage[first.end() : second.start()]
        if not re.search(r"\bfrom\b", before, re.IGNORECASE) or not re.search(
            r"\bto\b", between, re.IGNORECASE
        ):
            continue
        first_context = passage[max(0, first.start() - 10) : first.end() + 20]
        second_context = passage[max(0, second.start() - 10) : second.end() + 20]
        money = re.compile(r"₦|\$|\b(?:NGN|USD)\b|(?<!\w)N(?=\s*[\d,.])", re.IGNORECASE)
        if not money.search(first_context) or not money.search(second_context):
            continue
        first_value = Decimal(first["int"].replace(",", "") + (first["frac"] or ""))
        second_value = Decimal(second["int"].replace(",", "") + (second["frac"] or ""))
        if first["scale"]:
            first_value *= _SCALES[first["scale"].lower()]
        if second["scale"]:
            second_value *= _SCALES[second["scale"].lower()]
        found.add(
            "up"
            if second_value > first_value
            else "down"
            if second_value < first_value
            else "unchanged"
        )
    return next(iter(found)) if len(found) == 1 else None


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


def _word(raw: dict[str, Any], name: str, allowed: tuple[str, ...], neutral: str) -> str:
    value = raw.get(name)
    return value if isinstance(value, str) and value in allowed else neutral


def validate_claim(
    raw: dict[str, Any],
    document_text: str,
    published_at: datetime | None,
    allowed_item_codes: set[str],
    allowed_policy_series: set[str],
    allowed_item_units: dict[str, str] | None = None,
    allowed_policy_units: dict[str, str] | None = None,
) -> ValidatedClaim:
    passage = str(raw["passage"]).strip()
    span = find_passage(document_text, passage)
    value = _stated_value(raw.get("stated_value"))
    item_code = raw.get("item_code")
    policy_series = raw.get("policy_series")

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
    elif item_code is not None and (
        not isinstance(item_code, str) or item_code not in allowed_item_codes
    ):
        reason = UNKNOWN_ITEM_CODE
    elif policy_series is not None and (
        not isinstance(policy_series, str) or policy_series not in allowed_policy_series
    ):
        reason = UNKNOWN_POLICY_SERIES

    # The code's catalogue unit is authoritative. The model's free-text unit must agree with it,
    # and the selected number must be locally attached to that unit in the quoted passage.
    stated_unit = raw.get("stated_unit")
    item_units = allowed_item_units
    if item_units is None:
        from africasignal.catalog import load_items

        item_units = {item.code: item.unit for item in load_items().items}
    policy_units = allowed_policy_units
    if policy_units is None:
        from africasignal.policy_series import all_series

        policy_units = {series.code: series.unit for series in all_series()}
    expected_unit = (
        item_units.get(item_code)
        if isinstance(item_code, str)
        else policy_units.get(policy_series)
        if isinstance(policy_series, str)
        else None
    )
    checked_unit = expected_unit or (stated_unit if isinstance(stated_unit, str) else None)
    if reason is None and value is not None:
        if stated_unit is not None and not isinstance(stated_unit, str):
            reason = UNIT_MISMATCH
        elif (
            expected_unit is not None
            and stated_unit is not None
            and (
                _unit_dimension(stated_unit) != _unit_dimension(expected_unit)
                or (
                    _unit_currency(stated_unit) is not None
                    and _unit_currency(expected_unit) is not None
                    and _unit_currency(stated_unit) != _unit_currency(expected_unit)
                )
            )
        ):
            reason = UNIT_MISMATCH
        elif checked_unit is None or _unit_dimension(checked_unit) is None:
            reason = UNIT_NOT_IN_PASSAGE
        elif not _unit_appears_with_value(value, checked_unit, passage):
            reason = UNIT_NOT_IN_PASSAGE
        elif not _value_has_currency(value, _unit_currency(checked_unit), passage):
            reason = UNIT_MISMATCH
        elif _passage_currency_conflicts(value, _unit_currency(checked_unit), passage):
            reason = UNIT_MISMATCH

    claim_type = _word(raw, "claim_type", CLAIM_TYPES, "other")
    direction = _word(raw, "direction", DIRECTIONS, "unknown")
    time_precision = _word(raw, "time_precision", TIME_PRECISIONS, "unknown")
    if reason is None and (
        (claim_type, direction, time_precision)
        != (raw.get("claim_type"), raw.get("direction"), raw.get("time_precision"))
    ):
        reason = INVALID_FIELD

    direction = _word(raw, "direction", DIRECTIONS, "unknown")
    explicit_direction = _explicit_direction(passage)
    if direction in ("up", "down", "unchanged"):
        if explicit_direction is None:
            # Direction is useful as corroboration only when the source says it. Keep an
            # otherwise usable numeric claim, but do not let the model supply the trend.
            direction = "unknown"
        elif direction != explicit_direction:
            direction = "unknown"
            if reason is None:
                reason = DIRECTION_MISMATCH

    return ValidatedClaim(
        claim_type=claim_type,
        text=raw["text"],
        passage=passage,
        passage_start=span[0] if span else None,
        passage_end=span[1] if span else None,
        item_code=raw.get("item_code"),
        policy_series=raw.get("policy_series"),
        stated_value=value if value is not None and value.is_finite() else None,
        stated_unit=expected_unit or stated_unit,
        direction=direction,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        time_precision=time_precision,
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
    allowed_item_units: dict[str, str] | None = None,
    allowed_policy_units: dict[str, str] | None = None,
) -> list[ValidatedClaim]:
    return [
        validate_claim(
            r,
            document_text,
            published_at,
            allowed_item_codes,
            allowed_policy_series,
            allowed_item_units,
            allowed_policy_units,
        )
        for r in raws
    ]
