"""T1 observed price change (spec B8.2). Pure code: no database, no model calls, no clock reads.

Everything a reader sees about a price is computed here from measurements: month-on-month and
year-on-year change, materiality, severity, the national aggregates, the headline, the unknowns.
A language model may later write an explanation around these facts (B8.4); it never produces
them.

Rules, as implemented (``THRESHOLD`` is the item's materiality threshold from ``items.yaml``):

* ``mom_pct`` and ``yoy_pct`` are rounded half-up to one decimal, and every later rule uses the
  rounded figure, so a headline that says "rose 5.0 %" is material at a 5 % threshold.
* ``ratio`` is the larger of ``|mom_pct| / mom_threshold`` and ``|yoy_pct| / yoy_threshold`` over
  the changes that can be computed. The change is material when ``ratio >= 1``.
* Severity: ``none`` when not material, ``low`` for ratio below 2, ``medium`` for ratio from 2 up
  to and including 4, ``high`` above 4. Severity is ``none`` whenever the evidence is
  ``insufficient``.
* Evidence state: ``reported`` (the official measurement only; news claims do not exist yet) or
  ``insufficient`` when the previous month is missing or the latest period ended more than 120
  days before ``now``.
* ``valid_until`` is the end of the day 75 days after the latest period ends.
* A national situation (country scope) also gets the median of the state values, how many states
  rose, fell or stayed within +-0.5 %, and the three highest and lowest states.
* ``inputs_hash`` covers the template version, the item, the place and every input measurement
  (id and value), so the same inputs give the same hash and a new template version gives a new
  one.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

TEMPLATE_VERSION = "T1-1"
STALE_AFTER_DAYS = 120  # latest period older than this: insufficient evidence
VALID_FOR_DAYS = 75  # after the period ends: the next release plus a grace period
UNCHANGED_BAND_PCT = Decimal("0.5")  # a state within +-0.5 % is counted as unchanged
TOP_N = 3

EvidenceState = Literal["reported", "insufficient"]  # corroborated/disputed need claims (AS-025+)
Severity = Literal["none", "low", "medium", "high"]

_ONE_PLACE = Decimal("0.1")
_TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class PricePoint:
    """One current measurement: the value of an item at a place for a month."""

    measurement_id: int
    period_start: date
    period_end: date
    value: Decimal
    evidence_document_id: int
    source_label: str  # for example "Premium Motor Spirit (Petrol) Price Watch (October 2024)"


@dataclass(frozen=True)
class StatePoint:
    place_code: str
    place_name: str
    point: PricePoint


@dataclass(frozen=True)
class FactorSpec:
    code: str
    label: str


@dataclass(frozen=True)
class PriceInputs:
    item_code: str
    item_label: str  # for example "petrol (PMS)"
    unit: str
    mom_threshold_pct: Decimal
    yoy_threshold_pct: Decimal
    factors: tuple[FactorSpec, ...]
    place_code: str  # NG, NG-LA
    place_name: str  # Nigeria, Lagos
    place_kind: Literal["country", "state"]
    source_short: str  # for example "NBS"
    current: PricePoint  # the latest period P
    previous: PricePoint | None  # P - 1 month
    year_ago: PricePoint | None  # P - 12 months
    now: datetime  # passed in, so the same inputs at the same time give the same result
    states_current: tuple[StatePoint, ...] = ()  # national situations: every state at P
    states_previous: tuple[StatePoint, ...] = ()  # and at P - 1


@dataclass(frozen=True)
class PriceAssessment:
    """The computed content of one assessment version, before it is stored."""

    template_version: str
    inputs_hash: str
    evidence_state: EvidenceState
    severity: Severity
    material: bool
    ratio: Decimal | None
    mom_pct: Decimal | None
    yoy_pct: Decimal | None
    headline: str
    facts: list[dict[str, Any]]
    possible_factors: list[dict[str, Any]]
    unknowns: list[str]
    scope_label: str
    period_label: str
    period_start: date
    period_end: date
    last_checked_at: datetime
    valid_until: datetime
    # (kind, id) of every input used, for ``assessment_input`` (invalidation looks these up)
    inputs: list[tuple[str, int]]


# --- small pure helpers ------------------------------------------------------------------------


def pct_change(value: Decimal, base: Decimal) -> Decimal:
    """Percentage change from ``base`` to ``value``, rounded half-up to one decimal."""
    return ((value - base) / base * 100).quantize(_ONE_PLACE, rounding=ROUND_HALF_UP)


def _exact_pct(value: Decimal, base: Decimal) -> Decimal:
    return (value - base) / base * 100


def severity_for(ratio: Decimal | None) -> Severity:
    if ratio is None or ratio < 1:
        return "none"
    if ratio < 2:
        return "low"
    if ratio <= 4:
        return "medium"
    return "high"


def _naira(value: Decimal) -> str:
    return f"₦{value:,.2f}"


def place_phrase(kind: str, code: str, name: str) -> str:
    """How a place reads in a sentence."""
    if kind == "country":
        return "Nigeria"
    if code == "NG-FC":
        return "the Federal Capital Territory"
    return f"{name} State"


def _scope_label(kind: str, code: str, name: str, source_short: str) -> str:
    if kind == "country":
        return f"Nigeria (national average, {source_short})"
    if code == "NG-FC":
        return f"Federal Capital Territory (state average, {source_short})"
    return f"{name} State (state average, {source_short})"


def _json_number(value: Decimal) -> float:
    return float(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))


def _add_months(d: date, n: int) -> date:
    index = d.year * 12 + (d.month - 1) + n
    return date(index // 12, index % 12 + 1, 1)


# --- the computation ---------------------------------------------------------------------------


def _inputs_hash(inputs: PriceInputs, used: list[tuple[str, int, Decimal]]) -> str:
    doc = {
        "template": TEMPLATE_VERSION,
        "item": inputs.item_code,
        "place": inputs.place_code,
        "inputs": sorted({(kind, id_, str(value)) for kind, id_, value in used}),
    }
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _direction(mom: Decimal | None) -> Literal["rose", "fell", "unchanged", "unknown"]:
    if mom is None:
        return "unknown"
    if mom > 0:
        return "rose"
    if mom < 0:
        return "fell"
    return "unchanged"


def _headline(inputs: PriceInputs, mom: Decimal | None) -> str:
    place = place_phrase(inputs.place_kind, inputs.place_code, inputs.place_name)
    month = f"{inputs.current.period_start:%B %Y}"
    value = _naira(inputs.current.value)
    base = f"Average {inputs.item_label} price in {place}"
    match _direction(mom):
        case "rose" | "fell" as verb:
            assert mom is not None
            return f"{base} {verb} {abs(mom)}% in {month} to {value} ({inputs.source_short})"
        case "unchanged":
            return f"{base} was unchanged in {month} at {value} ({inputs.source_short})"
        case _:
            return (
                f"{base} was {value} in {month} ({inputs.source_short}); "
                "there is no previous month to compare with"
            )


def _national_aggregates(
    inputs: PriceInputs,
) -> tuple[list[dict[str, Any]], list[tuple[str, int, Decimal]]]:
    """Facts about the states behind a national average, and the measurements they used."""
    if inputs.place_kind != "country" or not inputs.states_current:
        return [], []
    period = f"{inputs.current.period_start:%B %Y}"
    source = inputs.current.source_label
    used = [("measurement", s.point.measurement_id, s.point.value) for s in inputs.states_current]
    docs = sorted({s.point.evidence_document_id for s in inputs.states_current})
    facts: list[dict[str, Any]] = [
        {
            "label": "Median of state averages",
            "value": _json_number(
                statistics.median([s.point.value for s in inputs.states_current])
            ),
            "unit": inputs.unit,
            "period": period,
            "place_code": inputs.place_code,
            "source_label": source,
            "evidence_ids": docs,
        }
    ]

    ranked = sorted(inputs.states_current, key=lambda s: (s.point.value, s.place_name))
    for label, chosen in (
        ("Highest states", list(reversed(ranked[-TOP_N:]))),
        ("Lowest states", ranked[:TOP_N]),
    ):
        facts.append(
            {
                "label": label,
                "value": [
                    {"place": s.place_name, "value": _json_number(s.point.value)} for s in chosen
                ],
                "unit": inputs.unit,
                "period": period,
                "place_code": inputs.place_code,
                "source_label": source,
                "evidence_ids": sorted({s.point.evidence_document_id for s in chosen}),
            }
        )

    before = {s.place_code: s.point for s in inputs.states_previous}
    up = down = flat = 0
    compared_docs: set[int] = set()
    for state in inputs.states_current:
        base = before.get(state.place_code)
        if base is None:
            continue
        used.append(("measurement", base.measurement_id, base.value))
        compared_docs |= {state.point.evidence_document_id, base.evidence_document_id}
        change = _exact_pct(state.point.value, base.value)
        if abs(change) <= UNCHANGED_BAND_PCT:
            flat += 1
        elif change > 0:
            up += 1
        else:
            down += 1
    for label, count in (("States up", up), ("States down", down), ("States unchanged", flat)):
        facts.append(
            {
                "label": label,
                "value": count,
                "unit": "states",
                "period": period,
                "place_code": inputs.place_code,
                "source_label": source,
                "evidence_ids": sorted(compared_docs),
            }
        )
    return facts, used


def _fact(
    label: str, value: Decimal, unit: str, point: PricePoint, place_code: str, period: str
) -> dict[str, Any]:
    return {
        "label": label,
        "value": _json_number(value),
        "unit": unit,
        "period": period,
        "place_code": place_code,
        "source_label": point.source_label,
        "evidence_ids": [point.evidence_document_id],
    }


def compute_price_change(inputs: PriceInputs) -> PriceAssessment:
    cur, prev, ago = inputs.current, inputs.previous, inputs.year_ago
    mom = pct_change(cur.value, prev.value) if prev else None
    yoy = pct_change(cur.value, ago.value) if ago else None

    ratios = [
        abs(change) / threshold
        for change, threshold in (
            (mom, inputs.mom_threshold_pct),
            (yoy, inputs.yoy_threshold_pct),
        )
        if change is not None
    ]
    ratio = max(ratios) if ratios else None
    material = ratio is not None and ratio >= 1

    age_days = (inputs.now.date() - cur.period_end).days
    stale = age_days > STALE_AFTER_DAYS
    evidence_state: EvidenceState = "insufficient" if prev is None or stale else "reported"
    severity: Severity = "none" if evidence_state == "insufficient" else severity_for(ratio)

    period = f"{cur.period_start:%B %Y}"
    facts = [_fact("Current price", cur.value, inputs.unit, cur, inputs.place_code, period)]
    used: list[tuple[str, int, Decimal]] = [("measurement", cur.measurement_id, cur.value)]
    if prev:
        used.append(("measurement", prev.measurement_id, prev.value))
        facts.append(
            _fact("Previous month", prev.value, inputs.unit, prev, inputs.place_code,
                  f"{prev.period_start:%B %Y}")
        )  # fmt: skip
    if ago:
        used.append(("measurement", ago.measurement_id, ago.value))
        facts.append(
            _fact("Same month last year", ago.value, inputs.unit, ago, inputs.place_code,
                  f"{ago.period_start:%B %Y}")
        )  # fmt: skip
    for label, change, base in (
        ("Month-on-month change", mom, prev),
        ("Year-on-year change", yoy, ago),
    ):
        if change is not None and base is not None:
            fact = _fact(label, change, "%", cur, inputs.place_code, period)
            fact["value"] = float(change)
            fact["evidence_ids"] = sorted({cur.evidence_document_id, base.evidence_document_id})
            facts.append(fact)

    national_facts, national_used = _national_aggregates(inputs)
    facts += national_facts
    used += national_used

    unknowns: list[str] = []
    if evidence_state == "insufficient" and prev is None:
        unknowns.append(f"The value for {_add_months(cur.period_start, -1):%B %Y} is not available")
    if stale:
        unknowns.append(
            f"The latest {inputs.source_short} figure is for {period}, "
            f"more than {STALE_AFTER_DAYS} days ago"
        )
    if ago is None:
        unknowns.append("Last year's value is not available")
    if inputs.place_kind == "state":
        unknowns.append(
            f"{inputs.source_short} averages are state-wide and may differ from prices in your LGA"
        )
    else:
        unknowns.append(
            f"{inputs.source_short} publishes a national average; prices differ between states "
            "and places"
        )
    if evidence_state == "reported":
        where = "this state" if inputs.place_kind == "state" else "Nigeria"
        unknowns.append(f"No independent report for {where} and month")

    possible_factors = [
        {"factor": f.label, "code": f.code, "status": "not_checked", "evidence_ids": []}
        for f in inputs.factors
    ]  # "supported" needs a linked claim; there are no claims yet

    return PriceAssessment(
        template_version=TEMPLATE_VERSION,
        inputs_hash=_inputs_hash(inputs, used),
        evidence_state=evidence_state,
        severity=severity,
        material=material,
        ratio=ratio,
        mom_pct=mom,
        yoy_pct=yoy,
        headline=_headline(inputs, mom),
        facts=facts,
        possible_factors=possible_factors,
        unknowns=unknowns,
        scope_label=_scope_label(
            inputs.place_kind, inputs.place_code, inputs.place_name, inputs.source_short
        ),
        period_label=period,
        period_start=cur.period_start,
        period_end=cur.period_end,
        last_checked_at=inputs.now,
        valid_until=datetime.combine(
            cur.period_end + timedelta(days=VALID_FOR_DAYS), time(23, 59, 59), tzinfo=UTC
        ),
        inputs=sorted({(kind, id_) for kind, id_, _ in used})
        + [("evidence_document", d) for d in _documents(facts)],
    )


def _documents(facts: list[dict[str, Any]]) -> list[int]:
    return sorted({doc for fact in facts for doc in fact["evidence_ids"]})
