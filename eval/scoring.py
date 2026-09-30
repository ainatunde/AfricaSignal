"""Compare pipeline results with the labels, and apply the launch-gate thresholds (AS-040).

The thresholds are in ``eval/thresholds.yaml`` and come from the plan (AS-040, "Accept when"). This
module implements the checks and reports pass or fail; it does not choose numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from eval.cases import Case
from eval.pipeline import CaseOutcome

THRESHOLDS_PATH = Path(__file__).resolve().parent / "thresholds.yaml"
ABSTAIN = {"withhold", "publish_insufficient", "none"}


def load_thresholds(path: Path = THRESHOLDS_PATH) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@dataclass
class Check:
    name: str
    ok: bool
    expected: Any = None
    actual: Any = None

    def describe(self) -> str:
        return f"{self.name}: expected {self.expected!r}, got {self.actual!r}"


@dataclass
class CaseScore:
    case_id: str
    template: str
    split: str
    gold: str
    tags: list[str]
    provenance: str
    decision: Check
    evidence_state: Check
    severity: Check
    place: Check
    date: Check
    numbers: list[Check]
    claims: list[Check]
    rules: Check | None
    factors: Check | None
    explanation: Check | None
    gdelt: Check | None
    finer_than_evidence: list[str]
    expected_abstention: bool
    abstained: bool
    error: str | None = None
    explanation_outcome: str | None = None
    unexpected_valid_claims: int = 0

    @property
    def passed(self) -> bool:
        return not self.failures()

    def failures(self) -> list[str]:
        out: list[str] = []
        if self.error:
            out.append(f"error: {self.error}")
        for check in (
            self.decision,
            self.evidence_state,
            self.severity,
            self.place,
            self.date,
            self.rules,
            self.factors,
            self.explanation,
            self.gdelt,
            *self.numbers,
            *self.claims,
        ):
            if check is not None and not check.ok:
                out.append(check.describe())
        out += [f"finer than evidence: {p}" for p in self.finer_than_evidence]
        return out


def expected_period_label(case: Case) -> str | None:
    """How the assessed period reads; None when the case expects no assessment at all."""
    d = case.expected.date
    if case.expected.decision == "none":
        return None
    if case.template == "T1":
        assert d is not None, "a T1 case with an assessment needs a date"
        return f"{d:%B %Y}"
    return "effective date not stated" if d is None else f"from {d.day} {d:%B %Y}"


def _fact_value(facts: list[dict[str, Any]], label: str) -> Any:
    return next((f.get("value") for f in facts if f.get("label") == label), None)


def score_case(case: Case, out: CaseOutcome) -> CaseScore:
    exp = case.expected
    version = out.version
    failed = out.error is not None
    state = version.evidence_state if version else "none"
    severity = version.severity if version else "none"

    # place: the scope, and every claim the labels say where it resolves
    place_ok = out.scope_place == exp.place
    place_actual: Any = out.scope_place
    by_passage = {(c.doc, c.passage): c for c in out.claims}
    claim_checks: list[Check] = []
    for c in exp.claims:
        got = by_passage.get((c.doc, c.passage))
        if got is None:
            claim_checks.append(Check(f"claim {c.doc}: {c.passage[:40]!r}", False, "present", None))
            if c.place is not None:
                place_ok = False
            continue
        actual = (got.valid, got.reason if not got.valid else None)
        expected = (c.valid, c.reason if not c.valid else None)
        claim_checks.append(
            Check(f"claim {c.doc}: {c.passage[:40]!r}", actual == expected, expected, actual)
        )
        if c.place is not None and got.place != c.place:
            place_ok = False
            place_actual = f"{out.scope_place}; claim {c.passage[:30]!r} -> {got.place}"
    known = {(c.doc, c.passage) for c in exp.claims}
    unexpected = sum(1 for c in out.claims if c.valid and (c.doc, c.passage) not in known)

    numbers: list[Check] = []
    for label, want in exp.numbers.items():
        got = _fact_value(version.facts, label) if version else None
        ok = isinstance(got, int | float) and abs(float(got) - want) < 1e-9
        numbers.append(Check(f"number {label}", ok, want, got))

    want_label = expected_period_label(case)
    got_label = version.period_label if version else None
    date_check = Check("date", not failed and got_label == want_label, want_label, got_label)

    rules = None
    if exp.rules is not None:
        rules = Check(
            "rules", sorted(out.rules) == sorted(exp.rules), sorted(exp.rules), sorted(out.rules)
        )

    factors = None
    if exp.supported_factors is not None:
        got_factors = sorted(version.possible_factors) if version else []
        factors = Check("supported factors", sorted(exp.supported_factors) == got_factors,
                        sorted(exp.supported_factors), got_factors)  # fmt: skip

    explanation = None
    if case.explanation is not None and out.provenance == "stand_in":
        got_outcome = out.explanation.outcome if out.explanation else "not asked"
        explanation = Check("explanation", got_outcome == case.explanation.expect,
                            case.explanation.expect, got_outcome)  # fmt: skip

    gdelt = None
    if case.gdelt_rows:
        want = [r.keep for r in case.gdelt_rows]
        gdelt = Check("gdelt filter", out.gdelt == want, want, out.gdelt)

    return CaseScore(
        case_id=case.id,
        template=case.template,
        split=case.split,
        gold=case.gold,
        tags=case.tags,
        provenance=out.provenance,
        decision=Check(
            "decision", not failed and out.decision == exp.decision, exp.decision, out.decision
        ),
        evidence_state=Check(
            "evidence state", not failed and state == exp.evidence_state, exp.evidence_state, state
        ),
        severity=Check("severity", not failed and severity == exp.severity, exp.severity, severity),
        place=Check("place", not failed and place_ok, exp.place, place_actual),
        date=date_check,
        numbers=numbers,
        claims=claim_checks,
        rules=rules,
        factors=factors,
        explanation=explanation,
        gdelt=gdelt,
        finer_than_evidence=out.finer_than_evidence,
        expected_abstention=exp.decision in ABSTAIN,
        abstained=out.decision in ABSTAIN,
        error=out.error,
        explanation_outcome=out.explanation.outcome if out.explanation else None,
        unexpected_valid_claims=unexpected,
    )


# --- aggregation -------------------------------------------------------------------------------


def _rate(correct: int, total: int) -> dict[str, Any]:
    return {"correct": correct, "total": total, "rate": (correct / total) if total else None}


def template_summary(scores: list[CaseScore]) -> dict[str, Any]:
    n = len(scores)
    number_checks = [c for s in scores for c in s.numbers]
    claim_checks = [c for s in scores for c in s.claims]
    explained = [s for s in scores if s.explanation_outcome is not None]
    return {
        "cases": n,
        "decision_accuracy": _rate(sum(s.decision.ok for s in scores), n),
        "evidence_state_accuracy": _rate(sum(s.evidence_state.ok for s in scores), n),
        "severity_accuracy": _rate(sum(s.severity.ok for s in scores), n),
        "place_accuracy": _rate(sum(s.place.ok for s in scores), n),
        "date_accuracy": _rate(sum(s.date.ok for s in scores), n),
        "number_accuracy": _rate(sum(c.ok for c in number_checks), len(number_checks)),
        "cases_with_every_number_right": _rate(
            sum(all(c.ok for c in s.numbers) for s in scores if s.numbers),
            sum(1 for s in scores if s.numbers),
        ),
        "claim_validity_accuracy": _rate(sum(c.ok for c in claim_checks), len(claim_checks)),
        "abstention": {
            "abstained": sum(s.abstained for s in scores),
            "expected": sum(s.expected_abstention for s in scores),
            "cases": n,
            "rate": (sum(s.abstained for s in scores) / n) if n else None,
            "expected_rate": (sum(s.expected_abstention for s in scores) / n) if n else None,
        },
        "explanations": {
            "asked": len(explained),
            "accepted": sum(s.explanation_outcome == "accepted" for s in explained),
            "accepted_after_retry": sum(
                s.explanation_outcome == "accepted_after_retry" for s in explained
            ),
            "none": sum(s.explanation_outcome == "none" for s in explained),
        },
        "unexpected_valid_claims": sum(s.unexpected_valid_claims for s in scores),
        "errors": sum(1 for s in scores if s.error),
    }


@dataclass
class GateCheck:
    id: str
    label: str
    threshold: Any
    value: Any
    passed: bool | None  # None: could not be computed
    counts: str = ""


@dataclass
class Report:
    summary: dict[str, Any] = field(default_factory=dict)
    gate: list[GateCheck] = field(default_factory=list)
    verdict: str = "incomplete"  # pass | fail | incomplete
    launch: dict[str, Any] = field(default_factory=dict)


def evaluate(cases: list[Case], scores: list[CaseScore], thresholds: dict[str, Any]) -> Report:
    by_template = {t: [s for s in scores if s.template == t] for t in ("T1", "T2")}
    report = Report()
    report.summary = {t: template_summary(s) for t, s in by_template.items()}
    report.summary["all"] = template_summary(scores)
    for split in ("dev", "test"):
        part = [s for s in scores if s.split == split]
        if part:
            report.summary[f"split:{split}"] = {
                t: template_summary([s for s in part if s.template == t]) for t in ("T1", "T2")
            }

    gate: list[GateCheck] = []
    numbers = report.summary["all"]["number_accuracy"]
    gate.append(
        GateCheck(
            "number_accuracy",
            "Number accuracy",
            thresholds["number_accuracy_min"],
            numbers["rate"],
            None if numbers["total"] == 0 else numbers["rate"] >= thresholds["number_accuracy_min"],
            f"{numbers['correct']}/{numbers['total']} numbers",
        )
    )
    violations = sum(len(s.finer_than_evidence) for s in scores)
    gate.append(
        GateCheck(
            "place_not_finer_than_evidence",
            "Published assessments with a place finer than their evidence",
            thresholds["published_place_finer_than_evidence_max"],
            violations,
            violations <= thresholds["published_place_finer_than_evidence_max"],
            f"{violations} problems in {sum(bool(s.finer_than_evidence) for s in scores)} cases",
        )
    )
    for t in ("T1", "T2"):
        acc = report.summary[t]["evidence_state_accuracy"]
        minimum = thresholds["evidence_state_accuracy_min"][t]
        gate.append(
            GateCheck(
                f"evidence_state_{t}",
                f"Evidence-state accuracy, {t}",
                minimum,
                acc["rate"],
                None if acc["total"] == 0 else acc["rate"] >= minimum,
                f"{acc['correct']}/{acc['total']} cases",
            )
        )
    report.gate = gate
    if any(g.passed is None for g in gate) or report.summary["all"]["errors"]:
        report.verdict = "incomplete"
    else:
        report.verdict = "pass" if all(g.passed for g in gate) else "fail"

    # A number from this run is a launch measurement only when it comes from the editorial
    # owner's labels, real model answers and a set of the required size.
    reasons: list[str] = []
    stand_in = sum(s.provenance == "stand_in" for s in scores)
    if stand_in:
        reasons.append(f"{stand_in} cases used hand-written stand-in model answers, not a model's")
    synthetic = sum(s.gold != "editorial" for s in scores)
    if synthetic:
        reasons.append(
            f"{synthetic} cases have synthetic labels written by the build, not the editorial owner"
        )
    for t, need in thresholds["reference_set_min_cases"].items():
        have = len(by_template[t])
        if have < need:
            reasons.append(f"the {t} set has {have} cases; the plan asks for at least {need}")
    if report.summary["all"]["errors"]:
        reasons.append(f"{report.summary['all']['errors']} cases raised errors")
    report.launch = {"is_launch_measurement": not reasons, "reasons": reasons}
    return report
