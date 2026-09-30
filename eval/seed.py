"""Generates the seed reference set: ``eval/cases/t1_seed.jsonl`` and ``eval/cases/t2_seed.jsonl``.

    python -m eval.seed          # rewrite the files
    python -m eval.seed --check  # fail if the files are not what this script produces

WHAT THIS IS. A starting set of synthetic cases. Every document is written (or generated) for the
set in the build's own words: nothing is copied from a news outlet, and the NBS and NERC
"documents" are stand-ins that say what they are. The labels (evidence state, severity, decision,
numbers, dates, places) are worked out here by hand-written arithmetic and by the rules in the
plan, NOT by running the assessment code, so a disagreement between the two is a finding. The
model's answers for each document are stand-ins too.

WHAT THIS IS NOT. The plan's reference set (AS-040, Part D2) is labelled by the editorial owner
from real documents. These cases cover the situations the plan lists and give the harness, CI and
the people who will tune the model something to run today; they are not a measurement of how the
product performs on real news. Cases labelled by a person belong in other files under
``eval/cases/`` with ``"gold": "editorial"``.

Families. Documents written for one item or series share wording, so a family is one "origin
group" and lives in one split. Stories that must count as independent reports use different
variants (different generated text); copies of one wire story use the same text.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from eval.cases import CASES_DIR, Case, check_set

D = Decimal

# --- wording -----------------------------------------------------------------------------------

_SUBJECTS = [
    "Dealers", "Marketers", "Station managers", "Commercial drivers", "Market women",
    "Transport union leaders", "Consumer advocates", "Analysts", "Officials", "Distributors",
    "Wholesalers", "Residents", "Shop owners", "Bus operators",
]  # fmt: skip
_VERBS = [
    "told reporters that", "said on Tuesday that", "complained that", "warned that",
    "explained that", "noted that", "argued that", "reported that", "insisted that",
    "told a radio station that", "said in a statement that", "added that",
]  # fmt: skip
_OBJECTS = [
    "customers had started to change how often they buy", "supply to smaller outlets arrived late",
    "deliveries were spread across the week", "small traders were cutting their stock",
    "many households were buying in smaller amounts", "queues formed in the early morning",
    "the state government had not commented", "some stalls opened later than usual",
    "transport fares were being reviewed", "weekly sales were uneven",
    "buyers compared offers at several outlets", "the picture differed from street to street",
    "older stock was being sold first", "a committee would meet later in the month",
]  # fmt: skip
_TAILS = [
    "and nobody expected a quick change", "although the detail was still unclear",
    "according to people present at the meeting", "as the month drew to a close",
    "while talks continued behind closed doors", "which surprised few observers",
    "as it had done the year before", "with attention on the coming weeks",
    "in a mood described as cautious", "after a quiet start to the week",
]  # fmt: skip


def body(family: str, variant: str, sentences: int = 7) -> str:
    """Generated filler prose: the same for the same (family, variant), different otherwise."""
    rng = random.Random(f"{family}:{variant}")
    return " ".join(
        f"{rng.choice(_SUBJECTS)} {rng.choice(_VERBS)} {rng.choice(_OBJECTS)} {rng.choice(_TAILS)}."
        for _ in range(sentences)
    )


# --- arithmetic for the labels (independent of the code under test) ----------------------------


def pct(value: float | str, base: float | str) -> Decimal:
    """Percentage change, rounded half-up to one decimal (plan B8.2)."""
    return ((D(str(value)) - D(str(base))) / D(str(base)) * 100).quantize(D("0.1"), ROUND_HALF_UP)


def severity_of(ratio: Decimal | None) -> str:
    if ratio is None or ratio < 1:
        return "none"
    if ratio < 2:
        return "low"
    if ratio <= 4:
        return "medium"
    return "high"


def months(period: str, back: int = 0) -> str:
    year, month = (int(p) for p in period.split("-"))
    index = year * 12 + month - 1 - back
    return f"{index // 12}-{index % 12 + 1:02d}"


def month_name(period: str) -> str:
    return datetime.strptime(period + "-01", "%Y-%m-%d").strftime("%B %Y")


def release_day(period: str) -> date:
    """NBS publishes about the middle of the following month."""
    year, month = (int(p) for p in months(period, -1).split("-"))
    return date(year, month, 12)


def stamp(day: date | str, hour: int = 9) -> str:
    d = date.fromisoformat(day) if isinstance(day, str) else day
    return f"{d.isoformat()}T{hour:02d}:00:00Z"


def two(base: float, change_pct: float, period: str = "2024-10") -> dict[str, float]:
    """A previous and a current value with (about) the given change."""
    return {months(period, 1): base, period: round(base * (1 + change_pct / 100), 2)}


def national_numbers(
    states: dict[str, dict[str, float]], period: str = "2024-10"
) -> dict[str, float]:
    """What a national situation says about its states (plan B8.2): the median of the state
    values and how many rose, fell or stayed within +-0.5 %."""
    current = [D(str(v[period])) for v in states.values()]
    up = down = flat = 0
    for v in states.values():
        before = D(str(v[months(period, 1)]))
        change = (D(str(v[period])) - before) / before * 100
        if abs(change) <= D("0.5"):
            flat += 1
        elif change > 0:
            up += 1
        else:
            down += 1
    return {
        "Median of state averages": float(statistics.median(current)),
        "States up": up,
        "States down": down,
        "States unchanged": flat,
    }


# --- T1 ----------------------------------------------------------------------------------------

NBS_TITLES = {
    "pms_litre": "Premium Motor Spirit (Petrol) Price Watch",
    "ago_litre": "Automotive Gas Oil (Diesel) Price Watch",
    "dpk_litre": "Household Kerosene Price Watch",
    "lpg_5kg": "Liquefied Petroleum Gas (Cooking Gas) Price Watch",
    "lpg_12_5kg": "Liquefied Petroleum Gas (Cooking Gas) Price Watch",
    "rice_local_1kg": "Selected Food Prices Watch",
}
# Per item: the family its news wording belongs to, the split that family lives in, and the words
# a story uses for the price and the unit.
ITEM_FAMILY = {
    "pms_litre": ("pms", "dev", "pump price of petrol", "litre"),
    "ago_litre": ("ago", "test", "price of diesel", "litre"),
    "dpk_litre": ("dpk", "dev", "price of household kerosene", "litre"),
    "lpg_5kg": ("lpg", "dev", "price of cooking gas", "5kg refill"),
    "lpg_12_5kg": ("lpg", "dev", "price of cooking gas", "12.5kg refill"),
    "rice_local_1kg": ("rice", "test", "price of local rice", "kg"),
}
VERB_FOR = {"up": "risen to", "down": "fallen to", "unchanged": "stayed at", "unknown": "moved to"}
NOW_DEFAULT = "2024-11-25T12:00:00Z"
PERIOD = "2024-10"


def claim(
    lead: str,
    *,
    item: str | None,
    direction: str = "up",
    value: float | None = None,
    places: list[str] | None = None,
    month: str | None = PERIOD,
    precision: str = "month",
    valid: bool = True,
    reason: str | None = None,
    place: str | None = None,
    passage: str | None = None,
    text: str | None = None,
    occurred_from: str | None = None,
) -> dict[str, Any]:
    """A claim the model is taken to have made about ``lead`` (its passage), and what the labels
    say should happen to it."""
    first = occurred_from or (f"{month}-01" if month else None)
    last = None
    if first and month and precision == "month":
        last = (date.fromisoformat(f"{months(month, -1)}-01") - timedelta(days=1)).isoformat()
    return {
        "answer": {
            "claim_type": "price_statement",
            "text": text or lead,
            "passage": passage or lead,
            "item_code": item,
            "policy_series": None,
            "stated_value": value,
            "stated_unit": None,
            "direction": direction,
            "occurred_from": first,
            "occurred_to": last,
            "time_precision": precision if first else "unknown",
            "place_candidates": places or [],
        },
        "expect": {"valid": valid, "reason": reason, "place": place},
        "lead": lead,
    }


def story_claim(
    item: str, *, place_word: str, price: int, direction: str = "up", **kwargs: Any
) -> dict[str, Any]:
    _, _, phrase, per = ITEM_FAMILY[item]
    lead = f"The {phrase} has {VERB_FOR[direction]} N{price:,} per {per} in {place_word}"
    kwargs.setdefault("places", [place_word])
    return claim(lead, item=item, direction=direction, value=float(price), **kwargs)


def news(
    source: str,
    variant: str,
    claims: list[dict[str, Any]],
    *,
    family: str,
    published: str = "2024-11-14",
    title: str = "Prices in the news",
    copy_of: int | None = None,
    append: str = "",
    withdrawn: bool = False,
    text: str | None = None,
) -> dict[str, Any]:
    return {
        "source": source, "variant": variant, "claims": claims, "family": family,
        "published": published, "title": title, "copy_of": copy_of, "append": append,
        "withdrawn": withdrawn, "text": text,
    }  # fmt: skip


class Builder:
    """Collects T1 cases."""

    def __init__(self) -> None:
        self.cases: list[dict[str, Any]] = []

    @staticmethod
    def _nbs_docs(item: str, releases: dict[tuple[str, str], str]) -> list[dict[str, Any]]:
        return [
            {
                "key": key,
                "source": "nbs",
                "title": f"{NBS_TITLES[item]} {month_name(period)}",
                "published": stamp(vintage),
                "text": (
                    f"Synthetic stand-in for the {NBS_TITLES[item]} for {month_name(period)}, "
                    f"released {vintage}. Average retail prices by state and nationally. "
                    "Written for the evaluation set; not an NBS publication."
                ),
            }
            for (period, vintage), key in releases.items()
        ]

    @staticmethod
    def _news_docs(
        specs: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        docs: list[dict[str, Any]] = []
        expected: list[dict[str, Any]] = []
        for index, n in enumerate(specs, 1):
            key = f"n{index}"
            doc: dict[str, Any] = {
                "key": key,
                "source": n["source"],
                "title": n["title"],
                "published": stamp(n["published"]),
                "withdrawn": n["withdrawn"],
            }
            if n["copy_of"] is not None:
                doc["copy_of"] = f"n{n['copy_of']}"
                doc["append"] = n["append"]
            else:
                leads = " ".join(c["lead"] + "." for c in n["claims"])
                doc["text"] = n["text"] or f"{leads} {body(n['family'], n['variant'])}"
            doc["answer"] = [c["answer"] for c in n["claims"]]
            docs.append(doc)
            for c in n["claims"]:
                expected.append({"doc": key, "passage": c["answer"]["passage"], **c["expect"]})
        return docs, expected

    def t1(
        self,
        case_id: str,
        *,
        item: str = "pms_litre",
        place: str = "NG-LA",
        values: dict[str, float] | None = None,  # period -> value at ``place``
        revisions: dict[str, tuple[float, str]] | None = None,  # period -> (value, vintage)
        states: dict[str, dict[str, float]] | None = None,  # place -> {period: value}
        news_docs: list[dict[str, Any]] | None = None,
        now: str = NOW_DEFAULT,
        state: str = "reported",
        decision: str | None = None,
        rules: list[str] | None = None,
        tags: list[str] | None = None,
        review_pending: str | None = None,  # a period whose value waits in the review queue
        withdraw_release: str | None = None,  # a period whose NBS release was withdrawn
        suspended: bool = False,
        supported_factors: list[str] | None = None,
        independent: int | None = None,
        disputing: int | None = None,
        extra_numbers: dict[str, float] | None = None,
        gdelt: list[dict[str, Any]] | None = None,
        expect_none: bool = False,
        period: str | None = None,
    ) -> None:
        family, split, *_ = ITEM_FAMILY[item]
        values = values or {}
        latest = period or max(values)
        releases: dict[tuple[str, str], str] = {}
        measurements: list[dict[str, Any]] = []

        def add(p: str, where: str, value: float, vintage: str | None = None, **extra: Any) -> None:
            v = vintage or release_day(p).isoformat()
            key = releases.setdefault((p, v), f"nbs-{p}-{v}")
            measurements.append(
                {"item": item, "place": where, "period": p, "value": value, "vintage": v,
                 "document": key} | extra
            )  # fmt: skip

        for p, value in values.items():
            add(p, place, value)
        for p, (value, vintage) in (revisions or {}).items():
            add(p, place, value, vintage)
        for where, series in (states or {}).items():
            for p, value in series.items():
                add(p, where, value)
        if review_pending:
            add(review_pending, place, values[latest] * 3, release_day(review_pending).isoformat(),
                review_pending=True)  # fmt: skip
        docs = self._nbs_docs(item, releases)
        for d in docs:
            if withdraw_release and d["key"].startswith(f"nbs-{withdraw_release}"):
                d["withdrawn"] = True
        news_list, expected_claims = self._news_docs(news_docs or [])
        docs += news_list

        # the labels, from the plan's rules
        current = revisions[latest][0] if revisions and latest in revisions else values[latest]
        previous = values.get(months(latest, 1))
        ago = values.get(months(latest, 12))
        mom = pct(current, previous) if previous else None
        yoy = pct(current, ago) if ago else None
        ratios = [abs(c) / t for c, t in ((mom, D(5)), (yoy, D(20))) if c is not None]
        ratio = max(ratios) if ratios else None
        numbers: dict[str, float] = {}
        if not expect_none:
            numbers["Current price"] = current
            if previous and mom is not None:
                numbers |= {"Previous month": previous, "Month-on-month change": float(mom)}
            if ago and yoy is not None:
                numbers |= {"Same month last year": ago, "Year-on-year change": float(yoy)}
            if independent is not None:
                numbers["Independent reports"] = independent
            if disputing is not None:
                numbers["Official statements that disagree"] = disputing
            numbers.update(extra_numbers or {})
        severity = "none" if state == "insufficient" else severity_of(ratio)
        if expect_none:
            decision_name, state, severity, rules = "none", "none", "none", []
        elif decision is not None:
            decision_name = decision
        elif state == "insufficient":
            decision_name, rules = "publish_insufficient", ["R3"]
        elif severity == "high":
            decision_name, rules = "hold", ["R7"]
        else:
            decision_name = "publish"
        expected: dict[str, Any] = {
            "decision": decision_name,
            "evidence_state": state,
            "severity": severity,
            "place": place,
            "date": None if expect_none else f"{latest}-01",
            "numbers": numbers,
            "rules": rules if rules is not None else [],
            "claims": expected_claims,
        }
        if supported_factors is not None:
            expected["supported_factors"] = supported_factors
        self.cases.append(
            {
                "id": case_id,
                "template": "T1",
                "split": split,
                "origin_group": f"t1-{family}",
                "tags": tags or [],
                "now": now,
                "situation": {"item": item, "place": place},
                "documents": docs,
                "measurements": measurements,
                "publication_suspended": suspended,
                **({"gdelt_rows": gdelt} if gdelt else {}),
                "expected": expected,
            }
        )


def build_t1() -> list[dict[str, Any]]:
    b = Builder()
    a, bb, c = "A", "B", "C"
    lagos = two(1000.88, 8.0)
    sc = story_claim
    # fmt: off

    # --- official figures only: severity bands, boundaries, rounding -------------------------
    b.t1("t1-bands-low", values=lagos, tags=["severity"])
    b.t1("t1-bands-none", values=two(1000.00, 2.0), tags=["severity"])
    b.t1("t1-bands-medium", place="NG-KN", values=two(980.00, 11.0), tags=["severity"])
    b.t1("t1-bands-high-held", place="NG-OY", values=two(1000.00, 27.0), tags=["severity", "r7"])
    b.t1("t1-bands-fell", place="NG-FC", values=two(1050.00, -6.5), tags=["severity"])
    b.t1("t1-boundary-five", item="ago_litre", values=two(1000.00, 5.0), tags=["severity", "boundary"])
    b.t1("t1-boundary-under-five", item="ago_litre", place="NG-RI", values=two(1000.00, 4.9), tags=["severity", "boundary"])
    b.t1("t1-boundary-two-x", item="ago_litre", place="NG-KN", values=two(1000.00, 10.0), tags=["severity", "boundary"])
    b.t1("t1-boundary-four-x", item="ago_litre", place="NG-OG", values=two(1000.00, 20.0), tags=["severity", "boundary"])
    b.t1("t1-boundary-over-four-x", item="ago_litre", place="NG-BE", values=two(1000.00, 20.1), tags=["severity", "boundary", "r7"])
    b.t1("t1-rounding-half-up", values={"2024-09": 1000.00, "2024-10": 1049.50}, tags=["rounding", "boundary"])
    b.t1("t1-yoy-triggers", item="dpk_litre", values={"2023-10": 800.00, "2024-09": 995.00, "2024-10": 1000.00}, tags=["severity", "yoy"])
    b.t1("t1-lpg-5kg", item="lpg_5kg", values={"2023-10": 5600.00, "2024-09": 6000.00, "2024-10": 6360.00}, tags=["severity", "yoy"])
    b.t1("t1-lpg-12kg-fell", item="lpg_12_5kg", place="NG-KN", values=two(14000.00, -12.0), tags=["severity"])
    b.t1("t1-rice-national", item="rice_local_1kg", place="NG", values=two(1200.00, 7.0), tags=["food", "national"])
    b.t1("t1-unchanged", item="dpk_litre", place="NG-RI", values=two(1150.00, 0.0), tags=["severity"])

    # --- missing months and stale data ---------------------------------------------------------
    b.t1("t1-one-month-only", values={PERIOD: 1080.95}, expect_none=True, tags=["insufficient"])
    b.t1("t1-stale", values=two(1000.00, 8.0, "2024-06"), state="insufficient", period="2024-06", tags=["stale", "insufficient"])
    b.t1("t1-stale-boundary-not-yet", values=two(1000.00, 8.0, "2024-07"), now="2024-11-28T12:00:00Z", period="2024-07", tags=["stale", "boundary"])
    b.t1("t1-stale-boundary-over", values=two(1000.00, 8.0, "2024-07"), now="2024-11-29T12:00:00Z", state="insufficient", period="2024-07", tags=["stale", "boundary", "insufficient"])

    # --- corroboration and its limits (petrol in Lagos, +8.0 %) ---------------------------------
    b.t1("t1-corroborated-one-outlet", values=lagos, state="corroborated", independent=1, tags=["corroboration"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Lagos", price=1081, place="NG-LA")], family="pms")])
    b.t1("t1-copied-wire-story", values=lagos, state="corroborated", independent=1, tags=["corroboration", "copied_wire"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Lagos", price=1081, place="NG-LA")], family="pms"),
        news("vanguard", a, [], family="pms", copy_of=1, append=" (Reuters)"),
        news("thisday", a, [], family="pms", copy_of=1, append=" (Agency report)")])
    b.t1("t1-two-independent-reports", values=lagos, state="corroborated", independent=2, tags=["corroboration"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Lagos", price=1081, place="NG-LA")], family="pms"),
        news("businessday", bb, [sc("pms_litre", place_word="Lagos", price=1079, place="NG-LA")], family="pms")])
    b.t1("t1-claim-about-another-state", values=lagos, tags=["corroboration", "place"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Oyo", price=1090, place="NG-OY")], family="pms")])
    b.t1("t1-claim-national-for-state", values=lagos, tags=["corroboration", "place"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Nigeria", price=1090, place="NG")], family="pms")])
    b.t1("t1-claim-other-item", values=lagos, tags=["corroboration"], news_docs=[
        news("punch", a, [sc("ago_litre", place_word="Lagos", price=1200, place="NG-LA")], family="pms")])
    b.t1("t1-news-says-opposite", values=lagos, tags=["corroboration", "opposite"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Lagos", price=950, direction="down", place="NG-LA")], family="pms")])
    b.t1("t1-official-says-opposite", values=lagos, state="disputed", disputing=1, tags=["disputed"], news_docs=[
        news("nmdpra", a, [sc("pms_litre", place_word="Lagos", price=950, direction="down", place="NG-LA")], family="pms", title="Depot price update")])
    b.t1("t1-claim-outside-window", values=lagos, tags=["corroboration", "dates"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Lagos", price=1000, month="2024-03", place="NG-LA")], family="pms")])
    b.t1("t1-claim-year-precision", values=lagos, tags=["corroboration", "dates"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Lagos", price=1081, month="2024", precision="year", occurred_from="2024-01-01", place="NG-LA")], family="pms")])
    b.t1("t1-claim-about-an-lga", values=lagos, state="corroborated", independent=1, tags=["corroboration", "place"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Ikeja", price=1085, place="NG-LA-ikeja")], family="pms")])
    b.t1("t1-ambiguous-place", values=lagos, tags=["place", "ambiguous_place"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Surulere", price=1085, place="ambiguous")], family="pms")])
    b.t1("t1-ambiguous-resolved-by-state", values=lagos, state="corroborated", independent=1, tags=["place", "ambiguous_place"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Surulere", price=1085, places=["Surulere", "Lagos"], place="NG-LA-surulere")], family="pms")])
    b.t1("t1-ambiguous-other-state", place="NG-OY", values=two(1000.00, 8.0), state="corroborated", independent=1, tags=["place", "ambiguous_place"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Surulere", price=1085, places=["Surulere", "Oyo State"], place="NG-OY-surulere")], family="pms")])
    b.t1("t1-claim-two-states", values=lagos, tags=["place", "ambiguous_place"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Lagos and Oyo", price=1085, places=["Lagos", "Oyo"], place="ambiguous")], family="pms")])
    b.t1("t1-niger-republic-not-niger-state", place="NG-NI", values=two(1000.00, 8.0), tags=["place", "niger_vs_nigeria", "gdelt"],
         news_docs=[news("punch", a, [claim("Fuel prices rose sharply in Niamey in October, petrol dealers said", item="pms_litre", places=["Niamey", "Niger Republic"], place="unknown")], family="pms", title="Prices across the border")],
         gdelt=[
             {"action_geo_country": "NG", "keep": False},  # FIPS NG is the Republic of Niger
             {"action_geo_country": "NI", "keep": True},  # FIPS NI is Nigeria
             {"actor1_country": "NGA", "keep": True},
             {"actor2_country": "NGA", "keep": True},
             {"action_geo_country": "NG", "actor1_country": "NER", "keep": False},
         ])
    b.t1("t1-niger-state-resolves", place="NG-NI", values=two(1000.00, 8.0), state="corroborated", independent=1, tags=["place", "niger_vs_nigeria"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Niger State", price=1081, place="NG-NI")], family="pms")])
    b.t1("t1-hallucinated-passage", values=lagos, tags=["hallucination"], news_docs=[
        news("punch", a, [claim("Petrol now sells for N1,400 per litre in Lagos", item="pms_litre", value=1400.0, places=["Lagos"], valid=False, reason="passage_not_found",
                                passage="Petrol stations across Lagos sold petrol at N1,400 per litre")],
             family="pms", text="Dealers discussed petrol supply in general terms. " + body("pms", "H"))])
    b.t1("t1-value-not-in-passage", values=lagos, tags=["hallucination"], news_docs=[
        news("punch", a, [claim("The pump price of petrol has risen in Lagos", item="pms_litre", value=1150.0, places=["Lagos"], valid=False, reason="value_not_in_passage")], family="pms")])
    b.t1("t1-future-date", values=lagos, tags=["dates", "hallucination"], news_docs=[
        news("punch", a, [claim("The pump price of petrol has risen in Lagos", item="pms_litre", places=["Lagos"], month="2025-01", valid=False, reason="future_date")], family="pms")])
    b.t1("t1-unknown-item-code", values=lagos, tags=["hallucination"], news_docs=[
        news("punch", a, [claim("The pump price of petrol has risen in Lagos", item="super_petrol", places=["Lagos"], valid=False, reason="unknown_item_code")], family="pms")])
    b.t1("t1-valid-and-hallucinated-claim", values=lagos, state="corroborated", independent=1, tags=["hallucination", "corroboration"], news_docs=[
        news("punch", a, [
            sc("pms_litre", place_word="Lagos", price=1081, place="NG-LA"),
            claim("Petrol will cost N2,000 per litre by December in Lagos", item="pms_litre", value=2000.0, places=["Lagos"], valid=False, reason="passage_not_found",
                  passage="Petrol will cost N2,000 per litre by December, marketers predicted")], family="pms")])
    b.t1("t1-withdrawn-article", values=lagos, tags=["withdrawn"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Lagos", price=1081, place="NG-LA")], family="pms", withdrawn=True)])
    nbs_text = (
        "Synthetic stand-in for the Premium Motor Spirit (Petrol) Price Watch. Average retail prices by state and "
        "nationally are published each month. The survey covers fuel stations in every state and the Federal Capital "
        "Territory. Enumerators record prices in person during the last full week of the month and the figures are "
        "reviewed before release. Users should treat state averages as indicative of the state, not of any single "
        "town. Written for the evaluation set; not an NBS publication.")
    b.t1("t1-outlet-reprints-official-text", values=lagos, tags=["copied_official", "corroboration"], news_docs=[
        news("punch", a, [], family="pms", title="Petrol price watch reprinted", text=nbs_text)])
    b.t1("t1-factors-supported", values=lagos, state="corroborated", independent=1, supported_factors=["exchange_rate", "supply_disruption"], tags=["factors"], news_docs=[
        news("punch", a, [claim("The pump price of petrol has risen to N1,081 per litre in Lagos as the exchange rate weakened and depot supply tightened",
                                item="pms_litre", value=1081.0, places=["Lagos"], place="NG-LA", passage="The pump price of petrol has risen to N1,081 per litre in Lagos")], family="pms")])
    b.t1("t1-factors-not-supported", values=lagos, state="corroborated", independent=1, supported_factors=[], tags=["factors"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Lagos", price=1081, place="NG-LA")], family="pms")])
    b.t1("t1-claim-says-unchanged", values=lagos, tags=["corroboration"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Lagos", price=1000, direction="unchanged", place="NG-LA")], family="pms")])
    b.t1("t1-claim-direction-unknown", values=lagos, tags=["corroboration"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Lagos", price=1050, direction="unknown", place="NG-LA")], family="pms")])
    b.t1("t1-abuja-alias", place="NG-FC", values=two(1050.00, 9.0), state="corroborated", independent=1, tags=["place"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Abuja", price=1145, place="NG-FC")], family="pms")])
    b.t1("t1-diesel-two-outlets", item="ago_litre", place="NG-KN", values=two(1100.00, 6.0), state="corroborated", independent=2, tags=["corroboration"], news_docs=[
        news("punch", a, [sc("ago_litre", place_word="Kano", price=1166, place="NG-KN")], family="ago"),
        news("dailytrust", bb, [sc("ago_litre", place_word="Kano", price=1170, place="NG-KN")], family="ago")])
    b.t1("t1-kerosene-yoy-corroborated", item="dpk_litre", values={"2023-10": 800.00, "2024-09": 995.00, "2024-10": 1000.00}, state="corroborated", independent=1, tags=["corroboration", "yoy"], news_docs=[
        news("punch", c, [sc("dpk_litre", place_word="Lagos", price=1000, place="NG-LA")], family="dpk")])
    b.t1("t1-gas-wire-and-independent", item="lpg_12_5kg", values=two(14000.00, 9.0), state="corroborated", independent=2, tags=["corroboration", "copied_wire"], news_docs=[
        news("punch", a, [sc("lpg_12_5kg", place_word="Lagos", price=15260, place="NG-LA")], family="lpg"),
        news("vanguard", a, [], family="lpg", copy_of=1, append=" (Agency)"),
        news("channels", bb, [sc("lpg_12_5kg", place_word="Lagos", price=15300, place="NG-LA")], family="lpg")])
    b.t1("t1-rice-corroborated-national", item="rice_local_1kg", place="NG", values=two(1200.00, 7.0), state="corroborated", independent=1, tags=["food", "national", "corroboration"], news_docs=[
        news("punch", a, [sc("rice_local_1kg", place_word="Nigeria", price=1284, place="NG")], family="rice")])

    # --- revisions, review queue, kill switch, withdrawn release ---------------------------------
    b.t1("t1-nbs-revision", values=two(1000.00, 8.0), revisions={PERIOD: (1050.00, "2024-12-10")}, now="2024-12-20T12:00:00Z", tags=["revision"])
    b.t1("t1-nbs-revision-not-material", values=two(1000.00, 8.0), revisions={PERIOD: (1020.00, "2024-12-10")}, now="2024-12-20T12:00:00Z", tags=["revision"])
    b.t1("t1-range-check-pending", values=two(1000.00, 8.0), review_pending="2024-11", now="2024-12-20T12:00:00Z", decision="withhold", rules=["R4"], tags=["review_queue", "r4"])
    b.t1("t1-kill-switch", values=two(1000.00, 8.0), suspended=True, decision="withhold", rules=["R1"], tags=["kill_switch", "r1"])
    b.t1("t1-official-release-withdrawn", values=two(1000.00, 8.0), withdraw_release=PERIOD, expect_none=True, tags=["withdrawn"])

    # --- national situations with state figures --------------------------------------------------
    state_series = {
        "NG-LA": two(1000.00, 6.0), "NG-OY": two(1000.00, 0.3), "NG-KN": two(1000.00, -2.0),
        "NG-RI": two(1000.00, 4.0), "NG-KO": two(1000.00, -0.5), "NG-PL": two(1000.00, 10.0),
    }
    national = two(1000.00, 3.0)
    aggregates = national_numbers(state_series)
    b.t1("t1-national-aggregates", place="NG", values=national, states=state_series, extra_numbers=aggregates, tags=["national"])
    b.t1("t1-national-corroborated", place="NG", values=national, states=state_series, extra_numbers=aggregates, state="corroborated", independent=1, tags=["national", "corroboration"], news_docs=[
        news("punch", a, [sc("pms_litre", place_word="Nigeria", price=1030, place="NG")], family="pms")])
    # fmt: on
    return b.cases


# --- T2 ----------------------------------------------------------------------------------------

IKEJA = "electricity_tariff_band_a:ikeja-electric"
EKO = "electricity_tariff_band_a:eko"
PMS_REG = "pms_regulated_price"
T2_NOW = "2025-10-20T12:00:00Z"
# series -> (split, origin group, scope place, default issuer, unit)
T2_SERIES = {
    IKEJA: ("dev", "t2-ikeja", "NG-LA", "nerc", "NGN/kWh"),
    EKO: ("test", "t2-eko", "NG-LA", "nerc", "NGN/kWh"),
    PMS_REG: ("test", "t2-pms", "NG", "nnpc", "NGN/litre"),
}


def order(
    series: str,
    rate: float,
    effective: str | None,
    published: str,
    *,
    precision: str = "month",
    variant: str = "x",
    suspends: bool = False,
    issuer: str | None = None,
    valid: bool = True,
    reason: str | None = None,
    value_override: float | None = None,
    phrasing: str | None = None,
    passage: str | None = None,
) -> dict[str, Any]:
    """An official document and the claim the model makes about it."""
    _, group, _, default_issuer, unit = T2_SERIES[series]
    who = issuer or default_issuer
    if series == PMS_REG:
        core = f"the pump price of petrol is N{rate:,.0f} per litre"
        what = f"The issuer announced that {core}"
        text_head = f"The announcement states that {core}"
    else:
        disco = "Ikeja Electric" if series == IKEJA else "Eko Electricity Distribution Company"
        core = f"a Band A tariff of N{rate:.2f}/kWh"
        what = f"NERC approved {core} for {disco}"
        text_head = f"The Commission approves {core} for customers of {disco}"
    passage_default = core
    if suspends and series == PMS_REG:
        text_head = "The Authority suspends the earlier announcement of the pump price of petrol"
        what = "The regulator suspended the earlier petrol price announcement"
        passage_default = "suspends the earlier announcement of the pump price of petrol"
    elif suspends:
        text_head = "The Commission suspends the earlier order on the Band A tariff"
        what = "NERC suspended the earlier Band A order"
        passage_default = "suspends the earlier order on the Band A tariff"
    when = ""
    if effective and precision in ("day", "month"):
        dd = date.fromisoformat(effective)
        when = f" The order takes effect on {dd.day} {dd.strftime('%B %Y')}."
    elif precision == "year":
        when = " The order applies during 2025."
    if not suspends and when:
        # The model is asked for a continuous stretch; a real one keeps the effective-date words
        # with the rate (validation rejects a future date on a passage without them).
        tail = f" for customers of {disco}" if series != PMS_REG else ""
        passage_default = f"{core}{tail}.{when}"
    text = f"{text_head}.{when} {body(f'{group}-official', variant)}"
    if phrasing:
        text = f"{phrasing}. {body(f'{group}-official', variant)}"
    answer = {
        "claim_type": "policy_statement",
        "text": what + ".",
        "passage": passage or passage_default,
        "item_code": None,
        "policy_series": series,
        "stated_value": None if suspends else (rate if value_override is None else value_override),
        "stated_unit": None if suspends else unit,
        "direction": "unknown",
        "occurred_from": effective,
        "occurred_to": None,
        "time_precision": precision if effective else "unknown",
        "place_candidates": [],
    }
    return {
        "source": who, "published": published, "title": f"Order of {published}", "text": text,
        "answer": answer, "valid": valid, "reason": reason,
    }  # fmt: skip


def report(
    series: str,
    source: str,
    published: str,
    *,
    rate: float | None = None,
    variant: str = "a",
    phrase: str | None = None,
    copy_of_order: int | None = None,
    append: str = "",
    hallucinated: bool = False,
) -> dict[str, Any]:
    """A news report about how the rate is applied. ``hallucinated``: the model's passage is not
    in the text, so the claim must be rejected."""
    _, group, *_ = T2_SERIES[series]
    if series == PMS_REG:
        lead = phrase or (
            f"Filling stations are now selling petrol at N{rate:,.0f} per litre"
            if rate
            else "Filling stations are now charging the new pump price of petrol"
        )
    else:
        lead = phrase or (
            f"Band A customers of the distribution company are now being charged N{rate:.2f}/kWh"
            if rate
            else "Band A customers are now being charged the new tariff"
        )
    text_lead = lead
    if hallucinated:
        text_lead = (
            "Band A customers and the distribution company discussed billing in general terms"
        )
    return {
        "news": True, "source": source, "published": published, "title": "Customers react",
        "lead": text_lead, "passage": lead, "variant": variant, "rate": rate,
        "copy_of_order": copy_of_order, "append": append, "valid": not hallucinated,
        "reason": "passage_not_found" if hallucinated else None, "family": group,
    }  # fmt: skip


def build_t2() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    def t2(
        case_id: str,
        series: str,
        items: list[dict[str, Any]],
        *,
        current: float | None = None,
        previous: float | None = None,
        effective: str | None = None,
        known_date: bool = True,
        state: str = "reported",
        announced: float | None = None,
        decision: str | None = None,
        rules: list[str] | None = None,
        independent: int | None = None,
        tags: list[str] | None = None,
        expect_none: bool = False,
        extra_numbers: dict[str, float] | None = None,
        suspended: bool = False,
    ) -> None:
        split, group, place, _, unit = T2_SERIES[series]
        docs: list[dict[str, Any]] = []
        expected_claims: list[dict[str, Any]] = []
        order_docs: dict[int, str] = {}
        for index, it in enumerate(items, 1):
            key = f"d{index}"
            doc: dict[str, Any] = {
                "key": key, "source": it["source"], "title": it["title"],
                "published": stamp(it["published"]),
            }  # fmt: skip
            if it.get("news"):
                reprinted = items[it["copy_of_order"] - 1] if it["copy_of_order"] else None
                if reprinted:
                    doc["copy_of"] = order_docs[it["copy_of_order"]]
                    doc["append"] = it["append"]
                else:
                    doc["text"] = f"{it['lead']}. {body(it['family'] + '-news', it['variant'])}"
                passage = reprinted["answer"]["passage"] if reprinted else it["passage"]
                value = reprinted["answer"]["stated_value"] if reprinted else it["rate"]
                answer = {
                    "claim_type": "policy_statement", "text": passage + ".", "passage": passage,
                    "item_code": None, "policy_series": series, "stated_value": value,
                    "stated_unit": unit if value else None, "direction": "unknown",
                    "occurred_from": None, "occurred_to": None, "time_precision": "unknown",
                    "place_candidates": [],
                }  # fmt: skip
            else:
                order_docs[index] = key
                doc["text"] = it["text"]
                answer = it["answer"]
            doc["answer"] = [answer]
            docs.append(doc)
            expected_claims.append(
                {"doc": key, "passage": answer["passage"], "valid": it.get("valid", True),
                 "reason": it.get("reason")}
            )  # fmt: skip
        numbers: dict[str, float] = {}
        severity = "none"
        if not expect_none:
            if current is not None:
                numbers["Current rate"] = current
            if announced is not None:
                numbers["Announced rate"] = announced
            if previous is not None and current is not None:
                numbers["Previous rate"] = previous
                step = abs(D(str(current)) - D(str(previous)))
                if step != 0:
                    label = "Increase in rate" if current > previous else "Decrease in rate"
                    numbers[label] = float(step)
                change = pct(current, previous)
                numbers["Change in rate"] = float(change)
                if state != "insufficient":
                    severity = severity_of(abs(change) / D(5))
                    if severity in ("medium", "high") and not known_date:
                        severity = "low"  # an unknown effective date caps severity (plan B8.3)
            if independent is not None:
                numbers["Independent reports that the rate is applied"] = independent
            numbers.update(extra_numbers or {})
        if expect_none:
            decision_name, state, rules = "none", "none", []
        elif decision is not None:
            decision_name = decision
        elif state == "insufficient":
            decision_name, rules = "publish_insufficient", rules or ["R3"]
        elif severity == "high":
            decision_name, rules = "hold", ["R7"]
        else:
            decision_name = "publish"
        cases.append(
            {
                "id": case_id,
                "template": "T2",
                "split": split,
                "origin_group": group,
                "tags": tags or [],
                "now": T2_NOW,
                "situation": {"series": series, "place": place},
                "documents": docs,
                "publication_suspended": suspended,
                "expected": {
                    "decision": decision_name,
                    "evidence_state": state,
                    "severity": severity,
                    "place": place,
                    "date": effective if (known_date and not expect_none) else None,
                    "numbers": numbers,
                    "rules": rules if rules is not None else [],
                    "claims": expected_claims,
                },
            }
        )

    o = order
    # fmt: off

    # --- one order, two orders, severity bands ---------------------------------------------------
    t2("t2-single-order", IKEJA, [o(IKEJA, 209.50, "2025-09-01", "2025-08-20")], current=209.50, effective="2025-09-01", tags=["single_order"])
    t2("t2-small-increase", IKEJA, [o(IKEJA, 206.80, "2025-03-01", "2025-02-15", variant="p"), o(IKEJA, 209.50, "2025-09-01", "2025-08-20", variant="q")],
       current=209.50, previous=206.80, effective="2025-09-01", tags=["severity"])
    for cid, prev, cur, tag in (
        ("t2-low-7", 209.50, 225.00, "severity"),
        ("t2-medium-14", 209.50, 240.00, "severity"),
        ("t2-high-28-held", 209.50, 269.40, "severity"),
        ("t2-boundary-five", 200.00, 210.00, "boundary"),
        ("t2-boundary-under-five", 200.00, 209.80, "boundary"),
        ("t2-boundary-two-x", 200.00, 220.00, "boundary"),
        ("t2-boundary-four-x", 200.00, 240.00, "boundary"),
        ("t2-boundary-over-four-x", 200.00, 240.40, "boundary"),
    ):
        t2(cid, IKEJA, [o(IKEJA, prev, "2025-03-01", "2025-02-15", variant="p"), o(IKEJA, cur, "2025-09-01", "2025-08-20", variant="q")],
           current=cur, previous=prev, effective="2025-09-01", tags=[tag, "severity"])
    t2("t2-decrease", IKEJA, [o(IKEJA, 225.00, "2025-03-01", "2025-02-15", variant="p"), o(IKEJA, 209.50, "2025-09-01", "2025-08-20", variant="q")],
       current=209.50, previous=225.00, effective="2025-09-01", tags=["severity"])
    t2("t2-unchanged-rate", IKEJA, [o(IKEJA, 209.50, "2025-03-01", "2025-02-15", variant="p"), o(IKEJA, 209.50, "2025-09-01", "2025-08-20", variant="q")],
       current=209.50, previous=209.50, effective="2025-09-01", tags=["severity"])
    t2("t2-eko-increase", EKO, [o(EKO, 198.00, "2025-03-01", "2025-02-15", variant="p"), o(EKO, 212.00, "2025-09-01", "2025-08-20", variant="q")],
       current=212.00, previous=198.00, effective="2025-09-01", tags=["eko"])
    t2("t2-three-orders-latest-two", IKEJA, [o(IKEJA, 180.00, "2025-01-01", "2024-12-18", variant="p"), o(IKEJA, 209.50, "2025-03-01", "2025-02-15", variant="q"), o(IKEJA, 225.00, "2025-09-01", "2025-08-20", variant="r")],
       current=225.00, previous=209.50, effective="2025-09-01", tags=["history"])
    t2("t2-published-out-of-order", IKEJA, [o(IKEJA, 225.00, "2025-09-01", "2025-08-20", variant="q"), o(IKEJA, 209.50, "2025-03-01", "2025-09-10", variant="p")],
       current=225.00, previous=209.50, effective="2025-09-01", tags=["history", "dates"])

    # --- corroboration by news, and what does not count -------------------------------------------
    two_orders = [o(IKEJA, 209.50, "2025-03-01", "2025-02-15", variant="p"), o(IKEJA, 240.00, "2025-09-01", "2025-08-20", variant="q")]
    base = {"current": 240.00, "previous": 209.50, "effective": "2025-09-01"}
    t2("t2-corroborated-wording", IKEJA, [*two_orders, report(IKEJA, "punch", "2025-09-15")], state="corroborated", independent=1, tags=["corroboration"], **base)
    t2("t2-corroborated-value", IKEJA, [*two_orders, report(IKEJA, "punch", "2025-09-15", rate=240.00)], state="corroborated", independent=1, tags=["corroboration"], **base)
    t2("t2-news-different-value", IKEJA, [*two_orders, report(IKEJA, "punch", "2025-09-15", rate=180.00)], tags=["corroboration"], **base)
    t2("t2-news-before-effective-date", IKEJA, [*two_orders, report(IKEJA, "punch", "2025-08-25")], tags=["corroboration", "dates"], **base)
    t2("t2-news-reprints-the-order", IKEJA, [*two_orders, report(IKEJA, "punch", "2025-09-15", copy_of_order=2, append=" (Punch)")], tags=["copied_official", "corroboration"], **base)
    t2("t2-copied-wire-reports", IKEJA, [*two_orders, report(IKEJA, "punch", "2025-09-15"), report(IKEJA, "vanguard", "2025-09-15"), report(IKEJA, "thisday", "2025-09-16")],
       state="corroborated", independent=1, tags=["corroboration", "copied_wire"], **base)
    t2("t2-two-independent-reports", EKO, [o(EKO, 198.00, "2025-03-01", "2025-02-15", variant="p"), o(EKO, 212.00, "2025-09-01", "2025-08-20", variant="q"),
                                            report(EKO, "punch", "2025-09-15", variant="a"),
                                            report(EKO, "businessday", "2025-09-18", variant="b", phrase="Residents on Band A say bills reflect the new tariff and they are now paying more")],
       current=212.00, previous=198.00, effective="2025-09-01", state="corroborated", independent=2, tags=["corroboration"])
    t2("t2-invalid-news-claim-ignored", IKEJA, [*two_orders, report(IKEJA, "punch", "2025-09-15", rate=240.00, hallucinated=True)], tags=["hallucination", "corroboration"], **base)

    # --- disputes, announcements, unknown dates ---------------------------------------------------
    t2("t2-later-order-suspends", IKEJA, [*two_orders, o(IKEJA, 0, "2025-09-20", "2025-09-19", suspends=True, variant="s")],
       state="disputed", extra_numbers={"Later official statements that suspend or reverse it": 1}, tags=["disputed", "policy_reversed"], **base)
    t2("t2-suspension-came-first", IKEJA, [o(IKEJA, 0, "2025-02-20", "2025-02-19", suspends=True, variant="s"), o(IKEJA, 209.50, "2025-03-01", "2025-02-25", variant="p"), o(IKEJA, 240.00, "2025-09-01", "2025-08-20", variant="q")],
       tags=["policy_reversed", "dates"], **base)
    t2("t2-announced-future-rate", IKEJA, [o(IKEJA, 209.50, "2025-03-01", "2025-02-15", variant="p"), o(IKEJA, 225.00, "2025-12-01", "2025-10-10", variant="q")],
       current=209.50, announced=225.00, effective="2025-03-01", tags=["announced"])
    t2("t2-only-announced-rate", IKEJA, [o(IKEJA, 225.00, "2025-12-01", "2025-10-10", variant="q")], announced=225.00, effective="2025-12-01", state="insufficient", tags=["announced", "insufficient"])
    t2("t2-effective-year-only", IKEJA, [o(IKEJA, 209.50, "2025-03-01", "2025-02-15", variant="p"), o(IKEJA, 240.00, "2025-01-01", "2025-08-20", precision="year", variant="q")],
       current=240.00, previous=209.50, effective=None, known_date=False, tags=["dates", "unknown_date"])
    t2("t2-two-documents-disagree", IKEJA, [o(IKEJA, 209.50, "2025-03-01", "2025-02-15", variant="p"), o(IKEJA, 240.00, "2025-09-01", "2025-08-20", variant="q"), o(IKEJA, 235.00, "2025-09-01", "2025-09-05", variant="r")],
       current=235.00, previous=209.50, effective="2025-09-01", tags=["disagreement"])

    # --- no primary document, nothing usable -------------------------------------------------------
    t2("t2-news-only", IKEJA, [report(IKEJA, "punch", "2025-09-15", rate=240.00)], state="insufficient", rules=["R5"], tags=["news_only", "insufficient", "r5"])
    t2("t2-no-claims", IKEJA, [], expect_none=True, tags=["empty"])
    t2("t2-model-value-not-in-passage", IKEJA, [o(IKEJA, 209.50, "2025-09-01", "2025-08-20", value_override=215.00, valid=False, reason="value_not_in_passage")], expect_none=True, tags=["hallucination"])
    t2("t2-hallucinated-passage", IKEJA, [o(IKEJA, 209.50, "2025-09-01", "2025-08-20", valid=False, reason="passage_not_found", passage="a Band A tariff of N300.00/kWh from next week")], expect_none=True, tags=["hallucination"])
    t2("t2-future-date-without-effective-words", IKEJA, [o(IKEJA, 209.50, "2025-09-01", "2025-08-20", valid=False, reason="future_date", passage="a Band A tariff of N209.50/kWh")], expect_none=True, tags=["dates", "validation_strictness"])
    t2("t2-tariff-not-read-by-code", IKEJA, [o(IKEJA, 209.50, "2025-09-01", "2025-08-20", valid=False, reason="tariff_value_mismatch",
                                               phrasing="Band A customers will pay 209.50 naira per kilowatt hour", passage="Band A customers will pay 209.50 naira per kilowatt hour")],
       expect_none=True, tags=["hallucination", "tariff_reconcile"])
    t2("t2-kill-switch", IKEJA, two_orders, suspended=True, decision="withhold", rules=["R1"], tags=["kill_switch", "r1"], **base)

    # --- petrol price announcements (NNPC, NMDPRA) -------------------------------------------------
    pms = [o(PMS_REG, 897, "2025-03-01", "2025-02-28", variant="p"), o(PMS_REG, 1030, "2025-09-01", "2025-08-30", variant="q")]
    pbase = {"current": 1030, "previous": 897, "effective": "2025-09-01"}
    t2("t2-pms-announcement", PMS_REG, pms, tags=["pms"], **pbase)
    t2("t2-pms-corroborated", PMS_REG, [*pms, report(PMS_REG, "punch", "2025-09-15", rate=1030)], state="corroborated", independent=1, tags=["pms", "corroboration"], **pbase)
    t2("t2-pms-station-price-differs", PMS_REG, [*pms, report(PMS_REG, "punch", "2025-09-15", rate=1100)], tags=["pms", "corroboration"], **pbase)
    t2("t2-pms-reversed", PMS_REG, [*pms, o(PMS_REG, 0, "2025-09-25", "2025-09-24", suspends=True, variant="s", issuer="nmdpra")],
       state="disputed", extra_numbers={"Later official statements that suspend or reverse it": 1}, tags=["pms", "disputed", "policy_reversed"], **pbase)
    t2("t2-pms-regulator-source", PMS_REG, [o(PMS_REG, 897, "2025-03-01", "2025-02-28", variant="p", issuer="nmdpra"), o(PMS_REG, 1030, "2025-09-01", "2025-08-30", variant="q", issuer="nmdpra")], tags=["pms"], **pbase)
    t2("t2-pms-news-only", PMS_REG, [report(PMS_REG, "punch", "2025-09-15", rate=1030)], state="insufficient", rules=["R5"], tags=["pms", "news_only", "insufficient", "r5"])
    # fmt: on
    return cases


# --- stand-in explanations ---------------------------------------------------------------------
# Answers a model is taken to have given when asked to explain an assessment, for a few cases: one
# that is fine, and several that the validator (plan B8.4) must turn away, one rule at a time.

# fmt: off
EXPLANATIONS: dict[str, dict[str, Any]] = {
    "t1-bands-low": {"expect": "accepted", "answers": [
        "The average petrol price in Lagos State moved up 8.0% in October 2024 to ₦1,080.95. This is a state-wide average from the National Bureau of Statistics, so prices near you may differ. No independent report for the month has been found yet."]},
    "t1-bands-medium": {"expect": "accepted_after_retry", "answers": [
        "Petrol prices in Kano State will keep rising after this 11.0% increase.",
        "The average petrol price in Kano State rose 11.0% in October 2024 to ₦1,087.80, according to the National Bureau of Statistics. This is a state-wide average."]},
    "t1-corroborated-one-outlet": {"expect": "accepted", "answers": [
        "Petrol in Lagos State rose 8.0% in October 2024, and one independent report points the same way."]},
    "t1-copied-wire-story": {"expect": "accepted_after_retry", "answers": [
        "Petrol in Lagos State rose 12.3% in October 2024.",
        "Petrol in Lagos State rose 8.0% in October 2024, and the report repeated by other outlets is counted once."]},
    "t1-boundary-five": {"expect": "none", "answers": [
        "Diesel prices in Kano State are also moving, and Oyo State saw a different change.",
        "Diesel in Lagos State moved 5.0% in October 2024 because of the exchange rate."]},
    "t1-factors-supported": {"expect": "accepted", "answers": [
        "Petrol in Lagos State rose 8.0% in October 2024 to ₦1,080.95. A report links the rise to the exchange rate and depot supply, so these factors are supported by an independent source."]},
    "t1-bands-none": {"expect": "accepted_after_retry", "answers": [
        "The price rose two percent, which is below the threshold for a notable change.",
        "Petrol in Lagos State moved 2.0% in October 2024, below the threshold for a notable change."]},
    "t1-bands-fell": {"expect": "accepted_after_retry", "answers": [
        "The price fell 6.5% in the Federal Capital Territory, see https://example.com for details.",
        "Petrol in the Federal Capital Territory fell 6.5% in October 2024, to ₦981.75, a state-wide average."]},
    "t2-corroborated-wording": {"expect": "accepted", "answers": [
        "NERC's order raises the Band A tariff for Ikeja Electric customers by 14.6% to ₦240.00 per kWh from 1 September 2025, and an independent report says customers are being charged it."]},
    "t2-low-7": {"expect": "accepted_after_retry", "answers": [
        "The regulator definitely raised the Band A tariff by 7.4% to ₦225.00 per kWh.",
        "NERC's order raises the Band A tariff for Ikeja Electric customers by 7.4% to ₦225.00 per kWh from 1 September 2025."]},
}
# fmt: on


def all_cases() -> list[dict[str, Any]]:
    cases = build_t1() + build_t2()
    for case in cases:
        if case["id"] in EXPLANATIONS:
            case["explanation"] = EXPLANATIONS[case["id"]]
    return cases


HEADER = "# Generated by `python -m eval.seed`; do not edit by hand. Synthetic seed cases: see eval/README.md."


def render(cases: list[dict[str, Any]], template: str) -> str:
    lines = [HEADER] + [
        json.dumps(c, ensure_ascii=False) for c in cases if c["template"] == template
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if the files are out of date")
    args = parser.parse_args(argv)
    cases = all_cases()
    problems = check_set([Case.model_validate(c) for c in cases])  # schema problems surface here
    if problems:
        print("the generated set is not fit to measure with:", *problems, sep="\n  - ")
        return 1
    files = {
        CASES_DIR / "t1_seed.jsonl": render(cases, "T1"),
        CASES_DIR / "t2_seed.jsonl": render(cases, "T2"),
    }
    stale = [p for p, text in files.items() if not p.exists() or p.read_text("utf-8") != text]
    if args.check:
        if stale:
            print("out of date (run python -m eval.seed):", *(p.name for p in stale), sep="\n  ")
            return 1
        print(f"{len(cases)} cases, up to date")
        return 0
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
    t1 = sum(c["template"] == "T1" for c in cases)
    print(f"wrote {len(cases)} cases ({t1} T1, {len(cases) - t1} T2) to {CASES_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
