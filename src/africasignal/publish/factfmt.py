"""How numbers in assessment facts read to people, and how to find them again in text.

Used by the public pages, the JSON API and the WhatsApp/X post generator so that every surface
writes a fact the same way. ``numbers_in`` is the other half: the post generator uses it to prove
that no number in a post is missing from the facts (AS-033).
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# Standalone numbers: "1,005.47", "3.2", "12". Not the digits inside "5kg", "12.5kg" or a word
# like "T1", not a pack size written "5 kg", and not a fragment of a longer number.
_NUMBER = re.compile(r"(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?!\w|[.,]\d|\s?kg\b)")


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _quantize(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def format_number(value: Any, places: int = 2) -> str:
    """``1005.47`` as ``1,005.47``."""
    return f"{_quantize(_dec(value), places):,.{places}f}"


def format_unit_suffix(unit: str) -> str:
    """``NGN/litre`` as ``per litre``, ``NGN/5kg`` as ``per 5kg``; empty when there is no per."""
    _, _, per = unit.partition("/")
    return f"per {per}" if per else ""


def format_value(value: Any, unit: str) -> str:
    """A fact's value with its unit: ``₦1,005.47 per litre``, ``3.2%``, ``42`` (a count)."""
    if unit == "%":
        return f"{format_number(value, 1)}%"
    if unit.startswith("NGN"):
        suffix = format_unit_suffix(unit)
        return f"₦{format_number(value, 2)}" + (f" {suffix}" if suffix else "")
    text = format_number(value, 0 if _dec(value) == _dec(value).to_integral_value() else 2)
    return f"{text} {unit}".strip()


def format_change(value: Any) -> str:
    """A percentage change with its sign: ``+3.2%``, ``-1.0%``, ``0.0%``."""
    dec = _quantize(_dec(value), 1)
    sign = "+" if dec > 0 else ""
    return f"{sign}{dec:,.1f}%"


def fact_value_text(fact: dict[str, Any]) -> str:
    """The value of a fact as shown in tables. Changes carry a sign; places in a list are named."""
    value = fact.get("value")
    unit = str(fact.get("unit") or "")
    if isinstance(value, list):
        return ", ".join(
            f"{item.get('place', '?')} {format_value(item.get('value'), unit)}"
            for item in value
            if isinstance(item, dict)
        )
    if value is None:
        return ""
    if unit == "%":
        return format_change(value)
    return format_value(value, unit)


def _variants(value: Decimal) -> set[str]:
    """Every plausible way a fact's value can be written: rounded to 0, 1 or 2 places, with and
    without thousands separators, with and without a sign."""
    out: set[str] = set()
    for places in (0, 1, 2):
        rounded = abs(_quantize(value, places))
        out.add(f"{rounded:.{places}f}")
        out.add(f"{rounded:,.{places}f}")
        if places and rounded == rounded.to_integral_value():
            out.add(f"{rounded:.0f}")
            out.add(f"{rounded:,.0f}")
    return out


def _walk_values(value: Any) -> list[Decimal]:
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, int | float | Decimal):
        return [_dec(value)]
    if isinstance(value, list):
        return [n for item in value for n in _walk_values(item)]
    if isinstance(value, dict):
        return [n for item in value.values() for n in _walk_values(item)]
    return []


def allowed_numbers(facts: list[dict[str, Any]]) -> set[str]:
    """Every spelling of every number a fact states: its value (or the values inside a list) and
    the numbers in its period and unit text, for example the 2026 of "August 2026"."""
    allowed: set[str] = set()
    for fact in facts:
        for number in _walk_values(fact.get("value")):
            allowed |= _variants(number)
        for key in ("period", "unit"):
            allowed |= set(numbers_in(str(fact.get(key) or "")))
    return allowed


def numbers_in(text: str) -> list[str]:
    """The standalone numbers in a text as written, with commas kept: ``["1,005.47", "3.2"]``."""
    return [m.group(1) + (m.group(2) or "") for m in _NUMBER.finditer(text)]
