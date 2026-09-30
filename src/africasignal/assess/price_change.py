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
* Evidence state (``insufficient`` first, then ``disputed``, then ``corroborated``):

  - ``insufficient``: the previous month is missing or the latest period ended more than 120 days
    before ``now``;
  - ``disputed``: a valid claim from an official, regulator or company source, about a moment in
    the period, says the price moved the opposite way (up against down) to the measurement;
  - ``corroborated``: a valid news claim from an origin independent of the measurement, about a
    moment within one month either side of the period, says the price moved the same way
    (``unchanged`` counts when the measurement is unchanged). Copies of one story are one origin,
    and a document in the same origin as the measurement is not independent of it;
  - ``reported``: the official measurement only.

  The claims given to ``compute_price_change`` are already matched to the item and the place
  (the place itself, or a finer place inside a state); this module judges direction, timing and
  independence. A news claim that says the opposite of the measurement does not make it disputed
  (only an official source can) but is listed under the unknowns.
* A possible factor is ``supported`` when a valid claim about the item and place, inside the same
  window as corroboration, mentions one of the factor's keywords. Nothing else makes it more than
  ``not_checked``, and no cause is ever stated as fact.
* ``valid_until`` is the end of the day 75 days after the latest period ends.
* A national situation (country scope) also gets the median of the state values, how many states
  rose, fell or stayed within +-0.5 %, and the three highest and lowest states.
* ``inputs_hash`` covers the template version, the item, the place, every input measurement
  (id and value) and every claim that shaped the result (id and origin), so the same inputs give
  the same hash and a new template version gives a new one.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from africasignal.assess.corroboration import (
    OPPOSITE,
    ClaimPoint,
    add_months,
    claim_text,
    document_ids,
    in_window,
    independent_origins,
    is_news,
    is_official,
    matches_any,
    month_end,
)

TEMPLATE_VERSION = "T1-2"
STALE_AFTER_DAYS = 120  # latest period older than this: insufficient evidence
VALID_FOR_DAYS = 75  # after the period ends: the next release plus a grace period
UNCHANGED_BAND_PCT = Decimal("0.5")  # a state within +-0.5 % is counted as unchanged
TOP_N = 3

EvidenceState = Literal["reported", "corroborated", "disputed", "insufficient"]
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
    origin_id: int | None = None  # the reporting origin of the document behind the value


@dataclass(frozen=True)
class StatePoint:
    place_code: str
    place_name: str
    point: PricePoint


@dataclass(frozen=True)
class FactorSpec:
    code: str
    label: str
    keywords: tuple[str, ...] = ()


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
    claims: tuple[ClaimPoint, ...] = ()  # valid claims matched to the item and the place


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


# --- the computation ---------------------------------------------------------------------------


def _inputs_hash(inputs: PriceInputs, used: list[tuple[str, int, Decimal | str]]) -> str:
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


_CLAIM_DIRECTION = {"rose": "up", "fell": "down", "unchanged": "unchanged"}


@dataclass(frozen=True)
class _Evidence:
    state: EvidenceState
    corroborating: tuple[ClaimPoint, ...] = ()  # news claims from independent origins
    disputing: tuple[ClaimPoint, ...] = ()  # official claims that say the opposite
    opposing_news: tuple[ClaimPoint, ...] = ()  # news claims that say the opposite


def _weigh_claims(inputs: PriceInputs, mom: Decimal | None, insufficient: bool) -> _Evidence:
    """The evidence state from the claims (spec B8.2). See the module docstring for the rules."""
    if insufficient or mom is None:
        return _Evidence("insufficient")
    measured = _CLAIM_DIRECTION[_direction(mom)]
    cur = inputs.current
    # Documents that share an origin with the measurement are not independent reports of it.
    own_origins = {
        p.origin_id for p in (inputs.current, inputs.previous) if p is not None and p.origin_id
    }
    window_start, window_end = (
        add_months(cur.period_start, -1),
        month_end(add_months(cur.period_start, 1)),
    )

    def same_origin(claim: ClaimPoint) -> bool:
        return claim.origin_id is not None and claim.origin_id in own_origins

    disputing = tuple(
        c
        for c in inputs.claims
        if is_official(c)
        and not same_origin(c)
        and c.direction == OPPOSITE.get(measured)
        and in_window(c, cur.period_start, cur.period_end)
    )
    news = [c for c in inputs.claims if is_news(c) and not same_origin(c) and not c.copies_official]
    news = [c for c in news if in_window(c, window_start, window_end)]
    corroborating = tuple(c for c in news if c.direction == measured)
    opposing = tuple(c for c in news if c.direction == OPPOSITE.get(measured))
    if disputing:
        return _Evidence("disputed", disputing=disputing, opposing_news=opposing)
    if corroborating:
        return _Evidence("corroborated", corroborating=corroborating, opposing_news=opposing)
    return _Evidence("reported", opposing_news=opposing)


def _supported_factors(inputs: PriceInputs) -> list[dict[str, Any]]:
    """Possible factors, each ``supported`` only by a valid claim that names it (spec B8.2)."""
    cur = inputs.current
    start, end = add_months(cur.period_start, -1), month_end(add_months(cur.period_start, 1))
    nearby = [c for c in inputs.claims if in_window(c, start, end)]
    factors: list[dict[str, Any]] = []
    for spec in inputs.factors:
        linked = [c for c in nearby if matches_any(claim_text(c), spec.keywords)]
        factor = _not_checked(spec)
        if linked:
            factor |= {
                "status": "supported",
                "evidence_ids": document_ids(linked),
                "claim_ids": sorted(c.claim_id for c in linked),
            }
        factors.append(factor)
    return factors


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
    weighed = _weigh_claims(inputs, mom, insufficient=prev is None or stale)
    evidence_state = weighed.state
    severity: Severity = "none" if evidence_state == "insufficient" else severity_for(ratio)

    period = f"{cur.period_start:%B %Y}"
    facts = [_fact("Current price", cur.value, inputs.unit, cur, inputs.place_code, period)]
    used: list[tuple[str, int, Decimal | str]] = [("measurement", cur.measurement_id, cur.value)]
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

    origins = independent_origins(weighed.corroborating)
    if origins:
        facts.append(
            _claim_fact(
                "Independent reports",
                len(origins),
                "reporting origins",
                weighed.corroborating,
                inputs.place_code,
                period,
            )
        )
    if weighed.disputing:
        facts.append(
            _claim_fact(
                "Official statements that disagree",
                len(weighed.disputing),
                "statements",
                weighed.disputing,
                inputs.place_code,
                period,
            )
        )
    possible_factors = (
        _supported_factors(inputs)
        if evidence_state != "insufficient"
        else [_not_checked(f) for f in inputs.factors]
    )
    supported_claims = {cid for factor in possible_factors for cid in factor.get("claim_ids", [])}
    used_claims = (
        {c.claim_id: c for c in (*weighed.corroborating, *weighed.disputing)}
        | {c.claim_id: c for c in inputs.claims if c.claim_id in supported_claims}
        | {c.claim_id: c for c in weighed.opposing_news}
    )
    used += [("claim", c.claim_id, str(c.origin_id)) for c in used_claims.values()]

    unknowns: list[str] = []
    if evidence_state == "insufficient" and prev is None:
        unknowns.append(f"The value for {add_months(cur.period_start, -1):%B %Y} is not available")
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
    if weighed.disputing:
        unknowns.append(
            "An official statement says the price moved the other way in this period; "
            "the figures above are the published measurement"
        )
    if weighed.opposing_news:
        unknowns.append(
            "A news report says the price moved the other way; it is not counted as confirmation"
        )

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
        + [("evidence_document", d) for d in _documents([*facts, *possible_factors])],
    )


def _documents(items: list[dict[str, Any]]) -> list[int]:
    return sorted({doc for item in items for doc in item["evidence_ids"]})


def _not_checked(spec: FactorSpec) -> dict[str, Any]:
    return {
        "factor": spec.label,
        "code": spec.code,
        "status": "not_checked",
        "evidence_ids": [],
    }


def _claim_fact(
    label: str,
    value: int,
    unit: str,
    claims: tuple[ClaimPoint, ...],
    place_code: str,
    period: str,
) -> dict[str, Any]:
    return {
        "label": label,
        "value": value,
        "unit": unit,
        "period": period,
        "place_code": place_code,
        "source_label": ", ".join(sorted({c.origin_label for c in claims})),
        "evidence_ids": document_ids(claims),
    }
