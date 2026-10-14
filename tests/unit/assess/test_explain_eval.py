"""A reproducible, synthetic validator evaluation set.

These cases measure deterministic rejection/acceptance behavior. They are not a substitute for
editorial scoring of real source-backed briefs or the release holdout sign-off.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from africasignal.assess.explain import ExplainInput, validate_assertions
from africasignal.assess.retrieval import RetrievedPassage
from africasignal.publish.factfmt import fact_value_text
from tests.unit.assess.test_explain import FACTS, data


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    split: str
    dimension: str
    expected: bool
    text: str
    fact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    negated_source: bool = False
    severity: str = "normal"


_POSITIVE_TEMPLATES = (
    "{source} reported {label} of {value} in {period}.",
    "For {period}, {source} recorded {label} as {value}.",
    "{label} was {value}, according to {source}, for {period}.",
    "The {label} figure in {period} was {value}.",
    "In {period}, {source} data showed {value} for {label}.",
    "The assessment records {value} for {label} in {period}.",
    "As of {period}, {source} listed {label} at {value}.",
    "Available {source} data puts {label} at {value} in {period}.",
    "{period}: {source} — {label}, {value}.",
    "The {period} figure from {source} is {value} for {label}.",
)

_ADVERSARIAL = {
    "units": (
        "The current price was ₦1,080.95 per kg in October 2024.",
        "The current price was ₦1,080.95 per kilogram in October 2024.",
        "The current price was ₦1,080.95 per 5kg in October 2024.",
        "The current price was ₦1,080.95 per litre and per kg in October 2024.",
        "The current price was N1,080.95 per kilowatt-hour in October 2024.",
    ),
    "dates": (
        "The current price was ₦1,080.95 per litre in November 2025.",
        "The current price was ₦1,080.95 per litre in March 2025.",
        "The current price was ₦1,080.95 per litre since 2025.",
        "The current price was ₦1,080.95 per litre on 2025-11-03.",
        "The current price was ₦1,080.95 per litre during 2025.",
    ),
    "directions": (
        "Prices fell 8.0% month-on-month.",
        "Prices dropped 8.0% compared with the previous month.",
        "Prices decreased 8.0% month-on-month.",
        "Prices were lower by 8.0% on the month.",
        "Prices declined 8.0% month-on-month.",
    ),
    "locations": (
        "The current price was ₦1,080.95 per litre in Kano in October 2024.",
        "The current price was ₦1,080.95 per litre in Abuja in October 2024.",
        "In Kano, the current price was ₦1,080.95 per litre in October 2024.",
        "Abuja recorded a current price of ₦1,080.95 per litre in October 2024.",
        "The October 2024 current price in Kano was ₦1,080.95 per litre.",
    ),
    "negation": (
        "According to NBS, the rise was caused by the exchange rate.",
        "NBS reported that the exchange rate explains the rise.",
        "The exchange rate contributed to the rise, according to NBS.",
        "According to NBS, the rise was linked to the exchange rate.",
        "NBS said the rise resulted from exchange rate changes.",
    ),
    "uncertainty": (
        "Petrol prices may rise next month.",
        "Prices might increase in the coming month.",
        "Prices could fall after this report.",
        "Prices are expected to rise.",
        "The next published value may decline.",
    ),
    "syndicated_reporting": (
        "Independent reports confirmed the change.",
        "The change was independently corroborated.",
        "Multiple outlets confirmed the change.",
        "Several sources agreed on this change.",
        "Both outlets independently verified the change.",
    ),
    "unsupported_implication": (
        "Every household became poorer as petrol costs rose.",
        "All households are worse off because of petrol prices.",
        "Rising petrol costs have made households poorer.",
        "Households are unable to afford food because prices rose.",
        "Many households are struggling after the price change.",
    ),
}


def evaluation_cases() -> list[EvalCase]:
    cases: list[EvalCase] = []
    for fact_index, fact in enumerate(FACTS):
        source = str(fact.get("source_label") or "NBS")
        for template_index, template in enumerate(_POSITIVE_TEMPLATES):
            cases.append(
                EvalCase(
                    case_id=f"P-{fact_index + 1:02}-{template_index + 1:02}",
                    split="development" if template_index < 5 else "held_out",
                    dimension=str(fact["label"]).lower().replace(" ", "_"),
                    expected=True,
                    text=template.format(
                        source=source,
                        label=str(fact["label"]).lower(),
                        value=fact_value_text(fact),
                        period=str(fact["period"]),
                    ),
                    fact_refs=(f"fact_{fact_index + 1}",),
                )
            )

    for dimension, texts in _ADVERSARIAL.items():
        for index, text in enumerate(texts):
            is_causal = dimension == "negation"
            cases.append(
                EvalCase(
                    case_id=f"H-{dimension[:3].upper()}-{index + 1:02}",
                    split="held_out",
                    dimension=dimension,
                    expected=False,
                    text=text,
                    fact_refs=("fact_4",) if dimension == "directions" else ("fact_1",),
                    evidence_refs=("evidence_1",) if is_causal else (),
                    negated_source=is_causal,
                    severity="high",
                )
            )
    return cases


def _case_input(case: EvalCase) -> ExplainInput:
    if not case.negated_source:
        return data()
    return data(
        possible_factors=[{"factor": "Exchange rate", "status": "supported", "claim_ids": [3]}],
        retrieved_evidence=[
            RetrievedPassage(
                claim_id=3,
                source="NBS",
                published="2024-10-01",
                passage="NBS said the rise was not caused by the exchange rate.",
            )
        ],
    )


def test_evaluation_set_has_120_cases_and_held_out_strata() -> None:
    cases = evaluation_cases()
    assert len(cases) == 120
    assert sum(case.split == "held_out" for case in cases) == 80
    assert sum(case.split == "held_out" and case.severity == "high" for case in cases) == 40
    assert sum(case.split == "held_out" and not case.expected for case in cases) == 40
    dimensions = {case.dimension for case in cases if case.split == "held_out"}
    assert {
        "current_price",
        "month-on-month_change",
        "year-on-year_change",
        "highest_states",
        *(_ADVERSARIAL.keys()),
    } <= dimensions


@pytest.mark.parametrize("case", evaluation_cases(), ids=lambda case: case.case_id)
def test_labeled_explanation_grounding_cases(case: EvalCase) -> None:
    raw = [
        {
            "text": case.text,
            "fact_refs": list(case.fact_refs),
            "evidence_refs": list(case.evidence_refs),
        }
    ]
    text, provenance, issues = validate_assertions(raw, _case_input(case))
    assert (not issues) is case.expected, f"{case.case_id}: {issues}"
    assert (text is not None) is case.expected
    assert bool(provenance) is case.expected
