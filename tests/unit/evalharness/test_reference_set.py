"""The evaluation reference set is well formed and its generator is up to date (AS-040)."""

from __future__ import annotations

from collections import Counter

import pytest

from eval import seed
from eval.cases import CASES_DIR, Case, check_set, load_cases
from eval.scoring import CaseScore, Check, evaluate, load_thresholds

PLAN_COVERAGE = [
    "copied_wire",
    "stale",
    "ambiguous_place",
    "niger_vs_nigeria",
    "revision",
    "policy_reversed",
    "hallucination",
]


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(CASES_DIR)


def test_set_has_no_problems(cases: list[Case]) -> None:
    assert check_set(cases) == []


def test_set_is_the_size_the_plan_asks_for(cases: list[Case]) -> None:
    by_template = Counter(c.template for c in cases)
    need = load_thresholds()["reference_set_min_cases"]
    assert by_template["T1"] >= need["T1"]
    assert by_template["T2"] >= need["T2"]


@pytest.mark.parametrize("tag", PLAN_COVERAGE)
def test_set_covers_what_the_plan_names(cases: list[Case], tag: str) -> None:
    assert any(tag in c.tags for c in cases)


def test_both_splits_are_used_for_both_templates(cases: list[Case]) -> None:
    assert {(c.template, c.split) for c in cases} == {
        ("T1", "dev"), ("T1", "test"), ("T2", "dev"), ("T2", "test"),
    }  # fmt: skip


def test_generated_files_are_up_to_date() -> None:
    """Edit eval/seed.py and run ``python -m eval.seed``; never edit the .jsonl files by hand."""
    assert seed.main(["--check"]) == 0


def test_a_split_leak_is_caught(cases: list[Case]) -> None:
    moved = [c.model_copy(deep=True) for c in cases]
    first_dev = next(c for c in moved if c.split == "dev")
    first_dev.split = "test"  # its origin group still has members in dev
    assert any("more than one split" in p for p in check_set(moved))


def test_a_duplicate_id_is_caught(cases: list[Case]) -> None:
    assert any("duplicate case id" in p for p in check_set([*cases, cases[0]]))


# --- the gate ----------------------------------------------------------------------------------


def _score(template: str, state_ok: bool = True, numbers_ok: bool = True, *, gold: str = "synthetic",
           provenance: str = "stand_in", finer: list[str] | None = None) -> CaseScore:  # fmt: skip
    ok = Check("x", True)
    return CaseScore(
        case_id="c", template=template, split="dev", gold=gold, tags=[], provenance=provenance,
        decision=ok, evidence_state=Check("evidence state", state_ok),
        severity=ok, place=ok, date=ok, numbers=[Check("number n", numbers_ok)], claims=[],
        rules=None, factors=None, explanation=None, gdelt=None,
        finer_than_evidence=finer or [], expected_abstention=False, abstained=False,
    )  # fmt: skip


def _scores(t1_bad: int = 0, t2_bad: int = 0) -> list[CaseScore]:
    return [_score("T1", i >= t1_bad) for i in range(60)] + [
        _score("T2", i >= t2_bad) for i in range(40)
    ]


def test_gate_passes_at_exactly_the_thresholds(cases: list[Case]) -> None:
    report = evaluate(cases, _scores(t1_bad=6, t2_bad=8), load_thresholds())
    assert report.verdict == "pass"


def test_gate_fails_one_case_under_the_t1_threshold(cases: list[Case]) -> None:
    report = evaluate(cases, _scores(t1_bad=7), load_thresholds())
    assert report.verdict == "fail"
    assert [g.id for g in report.gate if g.passed is False] == ["evidence_state_T1"]


def test_gate_fails_one_case_under_the_t2_threshold(cases: list[Case]) -> None:
    report = evaluate(cases, _scores(t2_bad=9), load_thresholds())
    assert [g.id for g in report.gate if g.passed is False] == ["evidence_state_T2"]


def test_one_wrong_number_fails_the_gate(cases: list[Case]) -> None:
    scores = _scores()
    scores[0].numbers.append(Check("number wrong", False, 1.0, 1.1))
    report = evaluate(cases, scores, load_thresholds())
    assert [g.id for g in report.gate if g.passed is False] == ["number_accuracy"]


def test_a_place_finer_than_its_evidence_fails_the_gate(cases: list[Case]) -> None:
    scores = _scores()
    scores[3].finer_than_evidence.append("Ikeja is finer than Lagos evidence")
    report = evaluate(cases, scores, load_thresholds())
    assert [g.id for g in report.gate if g.passed is False] == ["place_not_finer_than_evidence"]


def test_a_run_without_a_template_is_incomplete_not_passing(cases: list[Case]) -> None:
    report = evaluate(cases, [_score("T1") for _ in range(60)], load_thresholds())
    assert report.verdict == "incomplete"


def test_stand_in_and_synthetic_results_are_never_a_launch_measurement(cases: list[Case]) -> None:
    report = evaluate(cases, _scores(), load_thresholds())
    assert report.launch["is_launch_measurement"] is False
    assert any("stand-in" in r for r in report.launch["reasons"])
    assert any("synthetic" in r for r in report.launch["reasons"])


def test_editorial_labels_with_real_answers_are_a_launch_measurement(cases: list[Case]) -> None:
    scores = [
        _score(t, gold="editorial", provenance="live")
        for t, n in (("T1", 60), ("T2", 40))
        for _ in range(n)
    ]
    report = evaluate(cases, scores, load_thresholds())
    assert report.launch["is_launch_measurement"] is True
