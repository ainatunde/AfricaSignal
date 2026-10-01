"""T2 policy change (spec B8.3, AS-027). Pure code: no database, no model calls, no clock reads.

A policy situation follows one series (for example the Band A electricity tariff of one
distribution company). Its numbers come from valid ``policy_statement`` claims on primary
documents (regulator, government and company sources); news reports can only say whether the
change is being applied. Rules, as implemented:

* **Rates.** A primary claim with a stated value and no suspension wording is a rate. Its
  effective date is the claim's date when that is stated to the day or month; otherwise the day
  the document was published is used for ordering, the date is marked unknown, and severity is
  at most ``low``. When several documents give the same effective date, the newest document
  wins, and an unknown notes the disagreement if their values differ.
* **Current and previous.** The current rate is the latest rate whose effective date is not after
  ``now``; the previous rate is the latest one before that. The absolute and percentage change
  are computed here, never by a language model. A rate that takes effect after ``now`` is shown
  as an announced rate and does not replace the current one.
* **Severity.** ``ratio = |pct change| / materiality``. ``none`` below 1, ``low`` below 2,
  ``medium`` up to 4, ``high`` above (the same bands as T1); ``none`` when the evidence is
  ``insufficient``.
* **Evidence state** (``insufficient`` first, then ``disputed``, then ``corroborated``):

  - ``insufficient``: no primary rate is in force (no primary document, or only an announced
    future rate). A series with news reports only is assessed on those, attributed, as
    ``insufficient``;
  - ``disputed``: a primary claim published *after* the document that gave the current rate
    suspends, reverses, rescinds or withdraws it (``SUSPENSION`` words in its text);
  - ``corroborated``: a valid news claim from an independent origin, published on or after the
    effective date, reports implementation: it states the current value, or states no value and
    uses ``IMPLEMENTATION`` wording ("customers are now being charged"). Copies of one story are
    one origin, and a document in the same origin as a primary document is not independent;
  - ``reported``: a primary document only. The headline attributes it ("NERC says ...").

  The wording lists are deliberately short and in code, so a reader can check why a state was
  chosen; a claim they miss leaves the situation ``reported``, never ``corroborated``.
* **Affected groups** come from ``config/policies.yaml``. Nothing here infers them.
* ``valid_until`` is 45 days after the latest input is published, at the end of that day.
* ``inputs_hash`` covers the template version, the series, the place and every claim used (id,
  value, effective date or origin), so the same inputs give the same hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from africasignal.assess.corroboration import (
    DATED_PRECISIONS,
    ClaimPoint,
    claim_text,
    document_ids,
    independent_groups,
    is_news,
    is_official,
)
from africasignal.assess.price_change import (
    EvidenceState,
    Severity,
    pct_change,
    place_phrase,
    severity_for,
)

TEMPLATE_VERSION = "T2-1"
VALID_FOR_DAYS = 45  # after the latest input, then re-check

SUSPENSION = re.compile(
    r"\b(suspend(?:s|ed|ing)?|suspension|revers(?:e|es|ed|al)|rescind(?:s|ed)?|revok(?:e|es|ed)"
    r"|withdr(?:aw|aws|awn|ew)|cancel(?:s|led|ed)?|annul(?:s|led|ed)?)\b",
    re.IGNORECASE,
)
IMPLEMENTATION = re.compile(
    r"\b(?:now|are|is|being|been|were|was)\s+(?:paying|charg(?:ed|ing)|billed)\b"
    r"|\b(?:charges|charging|billing|implemented|implementation"
    r"|took effect|taken effect|taking effect|came into effect|come into effect|in effect)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PolicyInputs:
    series_code: str
    title: str  # for example "Electricity tariff, Band A, Ikeja Electric"
    unit: str  # for example "NGN/kWh"
    affected_groups: str
    materiality_pct: Decimal
    place_code: str  # the situation's scope: NG or a state
    place_name: str
    place_kind: Literal["country", "state"]
    claims: tuple[ClaimPoint, ...]  # valid policy_statement claims for the series
    now: datetime


@dataclass(frozen=True)
class PolicyAssessment:
    """The computed content of one T2 assessment version, before it is stored."""

    template_version: str
    inputs_hash: str
    evidence_state: EvidenceState
    severity: Severity
    material: bool
    change_pct: Decimal | None
    headline: str
    facts: list[dict[str, Any]]
    possible_factors: list[dict[str, Any]]
    unknowns: list[str]
    scope_label: str
    period_label: str
    effective_from: date | None
    has_primary_document: bool
    last_checked_at: datetime
    valid_until: datetime
    inputs: list[tuple[str, int]] = field(default_factory=list)


@dataclass(frozen=True)
class _Rate:
    claim: ClaimPoint
    value: Decimal
    effective: date
    known: bool  # the claim states the date to the day or month


def _rate_of(claim: ClaimPoint) -> _Rate | None:
    if claim.stated_value is None or SUSPENSION.search(claim_text(claim)):
        return None
    if claim.occurred_from is not None and claim.time_precision in DATED_PRECISIONS:
        return _Rate(claim, claim.stated_value, claim.occurred_from, True)
    return _Rate(claim, claim.stated_value, claim.published_at.date(), False)


def _day(d: date) -> str:
    return f"{d.day} {d:%B %Y}"


def _money(value: Decimal, unit: str) -> str:
    _, _, per = unit.partition("/")
    text = f"₦{value:,.2f}"
    return f"{text} per {per}" if per else text


def _newest(rates: list[_Rate]) -> _Rate:
    return max(rates, key=lambda r: (r.claim.published_at, r.claim.claim_id))


def _by_date(rates: list[_Rate]) -> list[list[_Rate]]:
    """Rates grouped by effective date, oldest date first."""
    groups: dict[date, list[_Rate]] = defaultdict(list)
    for rate in rates:
        groups[rate.effective].append(rate)
    return [groups[d] for d in sorted(groups)]


def _is_suspension(claim: ClaimPoint) -> bool:
    return SUSPENSION.search(claim_text(claim)) is not None


def _fact(
    label: str,
    value: Decimal | int,
    unit: str,
    period: str,
    place_code: str,
    claims: list[ClaimPoint],
) -> dict[str, Any]:
    outlets = sorted({c.source_name for c in claims})
    # News is named by outlet, so readers see who reported it; a primary document by its title.
    names = outlets if all(is_news(c) for c in claims) else sorted({c.origin_label for c in claims})
    return {
        "label": label,
        "value": float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        if isinstance(value, Decimal)
        else value,
        "unit": unit,
        "period": period,
        "place_code": place_code,
        "source_label": ", ".join(names),
        "outlets": outlets,
        "evidence_ids": document_ids(claims),
    }


def _hash(inputs: PolicyInputs, used: list[tuple[str, int, str]]) -> str:
    doc = {
        "template": TEMPLATE_VERSION,
        "series": inputs.series_code,
        "place": inputs.place_code,
        "inputs": sorted(set(used)),
    }
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def compute_policy_change(inputs: PolicyInputs) -> PolicyAssessment | None:
    """The assessment, or None when no claim gives anything to show (no rate and no news rate)."""
    today = inputs.now.date()
    official = [c for c in inputs.claims if is_official(c)]
    rates = [r for c in official if (r := _rate_of(c)) is not None]
    in_force = _by_date([r for r in rates if r.effective <= today])
    upcoming = _by_date([r for r in rates if r.effective > today])

    if not in_force and not upcoming:
        return _news_only(inputs)

    place = inputs.place_code
    facts: list[dict[str, Any]] = []
    used: list[tuple[str, int, str]] = []
    unknowns: list[str] = []
    claims_used: list[ClaimPoint] = []

    def use(rate: _Rate, agreeing: list[_Rate]) -> list[ClaimPoint]:
        claims = [r.claim for r in agreeing if r.value == rate.value]
        for claim in claims:
            used.append(("claim", claim.claim_id, f"{rate.value}@{rate.effective}"))
        claims_used.extend(claims)
        return claims

    for group in in_force + upcoming:
        if len({r.value for r in group}) > 1:
            unknowns.append(
                "Official documents state different rates for the same date; "
                "the most recently published is used"
            )
            break

    current = previous = None
    change_pct: Decimal | None = None
    if in_force:
        current = _newest(in_force[-1])
        current_claims = use(current, in_force[-1])
    if len(in_force) > 1:
        previous = _newest(in_force[-2])
        previous_claims = use(previous, in_force[-2])

    source_short = _newest(rates).claim.source_short if rates else ""
    period_label = "effective date not stated"
    effective_from: date | None = None
    severity: Severity = "none"
    material = False
    evidence_state: EvidenceState = "insufficient"
    headline = f"{inputs.title}: {source_short} has announced a new rate"

    if current is not None:
        effective_from = current.effective if current.known else None
        period_label = f"from {_day(current.effective)}" if current.known else period_label
        facts.append(
            _fact("Current rate", current.value, inputs.unit, period_label, place, current_claims)
        )
        if previous is not None:
            prev_period = f"from {_day(previous.effective)}" if previous.known else period_label
            facts.append(
                _fact("Previous rate", previous.value, inputs.unit, prev_period, place,
                      previous_claims)
            )  # fmt: skip
            if previous.value != 0:
                change_pct = pct_change(current.value, previous.value)
                step = abs(current.value - previous.value)
                if step != 0:
                    facts.append(
                        _fact(
                            "Increase in rate"
                            if current.value > previous.value
                            else "Decrease in rate",
                            step,
                            inputs.unit,
                            period_label,
                            place,
                            current_claims + previous_claims,
                        )  # fmt: skip
                    )
                pct_fact = _fact(
                    "Change in rate", change_pct, "%", period_label, place,
                    current_claims + previous_claims,
                )  # fmt: skip
                pct_fact["value"] = float(change_pct)
                facts.append(pct_fact)
            else:
                unknowns.append("The previous rate is zero, so no percentage change is given")
        else:
            unknowns.append("The rate before this one is not available")
        if not current.known:
            unknowns.append(
                "The date this rate takes effect is not stated; the publication date is used"
            )

        evidence_state = _evidence_state(inputs, current, rates, unknowns, facts, used, claims_used)
        if change_pct is not None and evidence_state != "insufficient":
            ratio = abs(change_pct) / inputs.materiality_pct
            material = ratio >= 1
            severity = severity_for(ratio)
            if severity in ("medium", "high") and not current.known:
                severity = "low"  # an unknown effective date caps severity (spec B8.3)
        headline = _headline(inputs, current, change_pct, source_short)
    else:
        unknowns.append("A new rate has been announced but is not in force yet")

    if upcoming:
        announced = _newest(upcoming[-1])
        announced_claims = use(announced, upcoming[-1])
        if current is not None:
            unknowns.append("A newer rate has been announced but is not in force yet")
        facts.append(
            _fact(
                "Announced rate",
                announced.value,
                inputs.unit,
                f"from {_day(announced.effective)}",
                place,
                announced_claims,
            )  # fmt: skip
        )
        if current is None:
            headline = (
                f"{inputs.title}: {source_short} has announced a rate of "
                f"{_money(announced.value, inputs.unit)} from {_day(announced.effective)}"
            )
            period_label = f"from {_day(announced.effective)}"
    if evidence_state == "reported":
        unknowns.append("No independent report that this rate is being applied")
    unknowns.append(
        "These are the rates in official documents; what individual customers are billed may differ"
    )

    latest_input = max(c.published_at.date() for c in claims_used)
    return PolicyAssessment(
        template_version=TEMPLATE_VERSION,
        inputs_hash=_hash(inputs, used),
        evidence_state=evidence_state,
        severity=severity,
        material=material,
        change_pct=change_pct,
        headline=headline,
        facts=facts,
        possible_factors=[],
        unknowns=unknowns,
        scope_label=_scope_label(inputs),
        period_label=period_label,
        effective_from=effective_from,
        has_primary_document=True,
        last_checked_at=inputs.now,
        valid_until=datetime.combine(
            latest_input + timedelta(days=VALID_FOR_DAYS), time(23, 59, 59), tzinfo=UTC
        ),
        inputs=_input_rows(used, facts),
    )


def _scope_label(inputs: PolicyInputs) -> str:
    place = place_phrase(inputs.place_kind, inputs.place_code, inputs.place_name)
    return f"{place} ({inputs.affected_groups})"


def _input_rows(
    used: list[tuple[str, int, str]], facts: list[dict[str, Any]]
) -> list[tuple[str, int]]:
    claims = sorted({(kind, id_) for kind, id_, _ in used})
    documents = sorted({doc for fact in facts for doc in fact["evidence_ids"]})
    return claims + [("evidence_document", d) for d in documents]


def _headline(inputs: PolicyInputs, current: _Rate, pct: Decimal | None, source: str) -> str:
    rate = _money(current.value, inputs.unit)
    since = f"from {_day(current.effective)}" if current.known else "(effective date not stated)"
    if pct is None:
        return (
            f"{inputs.title}: {source} says the rate is {rate} {since}; "
            "there is no previous rate to compare with"
        )
    if pct == 0:
        return f"{inputs.title}: {source} says the rate is unchanged at {rate} {since}"
    verb = "rose" if pct > 0 else "fell"
    return f"{inputs.title}: {source} says the rate {verb} {abs(pct)}% to {rate} {since}"


def _evidence_state(
    inputs: PolicyInputs,
    current: _Rate,
    rates: list[_Rate],
    unknowns: list[str],
    facts: list[dict[str, Any]],
    used: list[tuple[str, int, str]],
    claims_used: list[ClaimPoint],
) -> EvidenceState:
    """``disputed``, ``corroborated`` or ``reported`` for a rate that is in force."""
    place = inputs.place_code
    official = [c for c in inputs.claims if is_official(c)]
    doc_published = current.claim.published_at

    suspending = [
        c
        for c in official
        if _is_suspension(c)
        and c.published_at > doc_published
        and (c.occurred_from is None or c.occurred_from >= current.effective)
    ]
    if suspending:
        for claim in suspending:
            used.append(("claim", claim.claim_id, "suspension"))
        claims_used.extend(suspending)
        facts.append(
            _fact(
                "Later official statements that suspend or reverse it",
                len(suspending),
                "statements",
                f"after {_day(doc_published.date())}",
                place,
                suspending,
            )  # fmt: skip
        )
        unknowns.append(
            "A later official statement suspends or reverses this rate; "
            "the rate above is the last one published"
        )
        return "disputed"

    primary_origins = {c.origin_id for c in official if c.origin_id is not None}
    implementing = [
        c
        for c in inputs.claims
        if is_news(c)
        and not c.copies_official
        and c.origin_id not in primary_origins
        and current.known
        and c.published_at.date() >= current.effective
        and not _is_suspension(c)
        and (
            c.stated_value == current.value
            or (c.stated_value is None and IMPLEMENTATION.search(claim_text(c)) is not None)
        )
    ]
    if implementing:
        for claim in implementing:
            used.append(("claim", claim.claim_id, f"origin {claim.origin_id}"))
        claims_used.extend(implementing)
        facts.append(
            _fact(
                "Independent reports that the rate is applied",
                len(independent_groups(implementing)),
                "independent outlets",
                f"from {_day(current.effective)}",
                place,
                implementing,
            )  # fmt: skip
        )
        return "corroborated"
    return "reported"


def _news_only(inputs: PolicyInputs) -> PolicyAssessment | None:
    """No primary rate exists: list what news says, attributed, as insufficient evidence (R5)."""
    reports = [
        c
        for c in inputs.claims
        if is_news(c) and c.stated_value is not None and not _is_suspension(c)
    ]
    if not reports:
        return None
    facts = [
        _fact(
            f"Reported by {c.origin_label}",
            c.stated_value,  # type: ignore[arg-type]  # filtered above
            inputs.unit,
            f"published {_day(c.published_at.date())}",
            inputs.place_code,
            [c],
        )
        for c in sorted(reports, key=lambda c: (c.published_at, c.claim_id))
    ]
    used = [("claim", c.claim_id, f"origin {c.origin_id}") for c in reports]
    latest_input = max(c.published_at.date() for c in reports)
    return PolicyAssessment(
        template_version=TEMPLATE_VERSION,
        inputs_hash=_hash(inputs, used),
        evidence_state="insufficient",
        severity="none",
        material=False,
        change_pct=None,
        headline=f"{inputs.title}: no official document has been found yet",
        facts=facts,
        possible_factors=[],
        unknowns=[
            "No official document has been found for this rate; "
            "the figures above are news reports, not confirmed by the regulator",
        ],
        scope_label=_scope_label(inputs),
        period_label="effective date not stated",
        effective_from=None,
        has_primary_document=False,
        last_checked_at=inputs.now,
        valid_until=datetime.combine(
            latest_input + timedelta(days=VALID_FOR_DAYS), time(23, 59, 59), tzinfo=UTC
        ),
        inputs=_input_rows(used, facts),
    )
