"""Reading electricity tariffs out of NERC order text, in code (spec B6.4).

Tariff numbers are never taken from a language model. Two readers find them:

* ``band_a_table``: the "Approved Allowed Tariffs (₦/kWh)" table of a DisCo's monthly order, one row
  per tariff class and one column per period in force. The table is either typed text or OCR text.
  A row is refused if Tesseract was unsure of a digit in it, if the column headings and the values
  do not line up, or if the Band A rows disagree with each other (they are equal in every order
  seen, so a difference is a misread).
* ``find_amounts``: any ₦ or N amount followed by "/kWh" or "per kWh" in running text, for the prose
  of orders that state a tariff in a sentence, and for checking what a language model claimed
  (``tariff_values``).

The OCR turns ₦ into "&", "8" or nothing, so the table reader does not need the currency sign: it
needs the table's own "/kWh" title and numbers with two decimals.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

MIN_PLAUSIBLE = Decimal("1")  # NGN/kWh; the lifeline class is 4.00
MAX_PLAUSIBLE = Decimal("1000")

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name}
_MON = r"(?P<{}>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
_DASH = r"\s*[-–—]\s*"
_PERIOD = re.compile(
    # "Aug 2024 - Sep 2026" | "May - Jul 2024" | "Apr 2024"
    rf"{_MON.format('m1')}\.?\s+(?P<y1>\d{{4}}){_DASH}{_MON.format('m2')}\.?\s+(?P<y2>\d{{4}})"
    rf"|{_MON.format('n1')}\.?{_DASH}{_MON.format('n2')}\.?\s+(?P<ny>\d{{4}})"
    rf"|{_MON.format('s1')}\.?\s+(?P<sy>\d{{4}})",
    re.IGNORECASE,
)
_VALUE = re.compile(r"(?<![\d.,])\d{1,4}(?:,\d{3})*\.\d{2}(?![\d])")
_ROW = re.compile(
    r"^\s*A\s*[-–—]\s*(?P<cls>Non\s?-?\s?MD|MD\s?1|MD\s?2(?:\s+Special)?)\b(?P<rest>[\d.,|\s]+)$",
    re.IGNORECASE,
)
_TABLE_TITLE = re.compile(r"\bTariffs?\b.*\S/\s?kWh", re.IGNORECASE)
_TABLE_HEADER = re.compile(r"^\s*Tariff\s+Class\b(?P<periods>.*)$", re.IGNORECASE)
_AMOUNT = re.compile(
    r"(?<![A-Za-z])(?:₦|NGN\s?|N)\s?(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?:/|per\s+)\s?kWh\b",
    re.IGNORECASE,
)
_EFFECTIVE = re.compile(
    r"\b(?:take|takes|taking)\s+effect\s+(?:on|from)\s+(?:the\s+)?(?P<day>\d{1,2})\S{0,3}\s+"
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|"
    r"December)\s+(?P<year>\d{4})",
    re.IGNORECASE,
)
_BAND_A = re.compile(r"\bBand\s*\(?\s*A\s*\)?(?![\w-])", re.IGNORECASE)
_OTHER_BAND = re.compile(r"\bBands?\s*\(?\s*[B-E]\b", re.IGNORECASE)
_SENTENCE_BREAK = re.compile(r"(?<=[.;:])\s+|\n\s*\n")


@dataclass(frozen=True)
class Amount:
    """A ₦/kWh amount found in running text, with where it stands in the text."""

    value: Decimal
    start: int
    end: int


@dataclass(frozen=True)
class TariffColumn:
    """The Band A tariff over one period of a tariff table."""

    value: Decimal
    period_start: date
    period_end: date


@dataclass(frozen=True)
class BandATable:
    columns: list[TariffColumn]
    passage: str  # the "A - Non-MD" row as it stands in the text


@dataclass(frozen=True)
class TableRejected:
    reason: str


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def parse_periods(text: str) -> list[tuple[date, date]]:
    """The periods named in a table heading line, in the order written."""
    periods: list[tuple[date, date]] = []
    for m in _PERIOD.finditer(text):
        if m["y1"]:
            a, b, ya, yb = m["m1"], m["m2"], int(m["y1"]), int(m["y2"])
        elif m["ny"]:
            a, b, ya, yb = m["n1"], m["n2"], int(m["ny"]), int(m["ny"])
        else:
            a = b = m["s1"]
            ya = yb = int(m["sy"])
        start_month, end_month = _MONTHS[a[:3].lower()], _MONTHS[b[:3].lower()]
        start, end = date(ya, start_month, 1), _month_end(yb, end_month)
        if end < start:
            continue
        periods.append((start, end))
    return periods


def find_amounts(text: str) -> list[Amount]:
    """Every ₦/N/NGN amount followed by "/kWh" or "per kWh". "N" counts only as a whole word, so
    the N in "MW" or "kWh" is never read as naira."""
    found = []
    for m in _AMOUNT.finditer(text):
        found.append(Amount(Decimal(m["num"].replace(",", "")), m.start(), m.end()))
    return found


def effective_date(text: str) -> date | None:
    """The date an order says it takes effect ("shall take effect on 1st September 2026")."""
    m = _EFFECTIVE.search(text)
    if m is None:
        return None
    month = list(calendar.month_name).index(m["month"].capitalize())
    try:
        return date(int(m["year"]), month, int(m["day"]))
    except ValueError:
        return None


def band_a_table(
    text: str, doubtful_lines: Collection[str] = ()
) -> BandATable | TableRejected | None:
    """The Band A row of the ₦/kWh tariff table in ``text``.

    Returns None when the text has no such table, ``TableRejected`` (with the reason) when it has
    one that cannot be trusted, and the parsed row otherwise.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not _TABLE_TITLE.search(line):
            continue
        outcome = _read_table(lines, i + 1, set(doubtful_lines))
        if outcome is not None:
            return outcome
    return None


def _read_table(
    lines: list[str], first: int, doubtful: set[str]
) -> BandATable | TableRejected | None:
    # the heading row follows the title within a couple of lines
    header_at = next(
        (j for j in range(first, min(first + 3, len(lines))) if _TABLE_HEADER.match(lines[j])),
        None,
    )
    if header_at is None:
        return None
    header = _TABLE_HEADER.match(lines[header_at])
    assert header is not None
    periods = parse_periods(header["periods"])
    if not periods:
        return TableRejected("the table's column headings are not periods")

    rows: dict[str, tuple[str, list[Decimal]]] = {}
    for line in lines[header_at + 1 : header_at + 40]:
        row = _ROW.match(line)
        if row is None:
            if rows and line.strip():
                break  # past the Band A rows
            continue
        cls = re.sub(r"[\s-]+", "", row["cls"]).upper()
        if line in doubtful:
            return TableRejected(f"a digit in the Band A {row['cls']} row was read with doubt")
        values = [Decimal(v.replace(",", "")) for v in _VALUE.findall(row["rest"])]
        if len(values) != len(periods):
            return TableRejected(
                f"the Band A {row['cls']} row has {len(values)} values for {len(periods)} columns"
            )
        rows[cls] = (line.strip(), values)
    if "NONMD" not in rows:
        return None  # a table of something else (no Band A row)
    first_values = rows["NONMD"][1]
    if any(values != first_values for _, values in rows.values()):
        return TableRejected("the Band A rows disagree with each other")
    if any(not MIN_PLAUSIBLE <= v <= MAX_PLAUSIBLE for v in first_values):
        return TableRejected("a Band A value is outside the plausible range")
    return BandATable(
        columns=[TariffColumn(v, s, e) for v, (s, e) in zip(first_values, periods, strict=True)],
        passage=rows["NONMD"][0],
    )


@dataclass(frozen=True)
class BandAStatement:
    """A sentence stating a Band A tariff in running text."""

    value: Decimal
    passage: str
    start: int


def band_a_statements(text: str) -> list[BandAStatement]:
    """Sentences that put a ₦/kWh amount on Band A and on no other band."""
    found = []
    position = 0
    for piece in _SENTENCE_BREAK.split(text):
        start = text.find(piece, position)
        if start < 0:
            continue
        position = start + len(piece)
        if not _BAND_A.search(piece) or _OTHER_BAND.search(piece):
            continue
        amounts = {a.value for a in find_amounts(piece)}
        if len(amounts) == 1 and MIN_PLAUSIBLE <= next(iter(amounts)) <= MAX_PLAUSIBLE:
            found.append(BandAStatement(next(iter(amounts)), piece.strip(), start))
    return found


def tariff_values(text: str, doubtful_lines: Collection[str] = ()) -> set[Decimal]:
    """Every tariff figure code can read in ``text``: the Band A table and ₦/kWh amounts in prose.
    A language model's ``stated_value`` for a tariff claim must be one of these."""
    values = {a.value for a in find_amounts(text)}
    table = band_a_table(text, doubtful_lines)
    if isinstance(table, BandATable):
        values |= {c.value for c in table.columns}
    return values
