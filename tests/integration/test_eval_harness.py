"""The evaluation harness runs offline on the reference set and catches a wrong label (AS-040)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import Engine

from eval import harness
from eval.cases import Case, load_cases
from eval.pipeline import Models
from eval.scoring import evaluate, load_thresholds


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases()


def test_the_fixture_subset_runs_offline_and_matches_its_labels(
    engine: Engine, cases: list[Case]
) -> None:
    # A few cases of every kind keeps this quick; `python -m eval.harness` runs them all.
    chosen: list[Case] = []
    for tag in ("copied_wire", "stale", "ambiguous_place", "niger_vs_nigeria", "revision",
                "policy_reversed", "hallucination", "r7", "corroboration"):  # fmt: skip
        chosen.append(next(c for c in cases if tag in c.tags))
    chosen += [
        next(c for c in cases if c.template == t and c.split == "test") for t in ("T1", "T2")
    ]
    chosen = list({c.id: c for c in chosen}.values())

    _, scores = harness.run_cases(engine, chosen, Models(mode="stand_in"))

    failures = {s.case_id: s.failures() for s in scores if not s.passed}
    assert failures == {}
    report = evaluate(chosen, scores, load_thresholds())
    assert report.launch["is_launch_measurement"] is False  # stand-in answers, synthetic labels


def test_a_wrong_label_is_reported_not_absorbed(engine: Engine, cases: list[Case]) -> None:
    case = next(c for c in cases if c.template == "T1" and c.expected.numbers)
    wrong = case.model_copy(deep=True)
    label = next(iter(wrong.expected.numbers))
    wrong.expected.numbers[label] += 0.1
    wrong.expected.evidence_state = (
        "disputed" if case.expected.evidence_state != "disputed" else "reported"
    )

    _, scores = harness.run_cases(engine, [wrong], Models(mode="stand_in"))

    assert not scores[0].passed
    report = evaluate([wrong], scores, load_thresholds())
    assert any(g.id == "number_accuracy" and g.passed is False for g in report.gate)


def test_a_case_that_raises_is_an_error_result_and_does_not_stop_the_run(
    engine: Engine, cases: list[Case]
) -> None:
    broken = cases[0].model_copy(deep=True)
    broken.id = "broken"
    broken.documents[0].source = "no-such-source"
    healthy = cases[1]

    outcomes, scores = harness.run_cases(engine, [broken, healthy], Models(mode="stand_in"))

    assert outcomes[0].error and "no-such-source" in outcomes[0].error
    assert scores[1].passed


def test_cases_leave_the_database_untouched(engine: Engine, cases: list[Case]) -> None:
    from sqlalchemy import text

    tables = (
        "evidence_document",
        "claim",
        "measurement",
        "situation",
        "assessment_version",
        "place",
    )

    def rows() -> list[int]:
        with engine.connect() as conn:
            return [
                int(conn.execute(text(f"select count(*) from {t}")).scalar_one()) for t in tables
            ]

    before = rows()
    harness.run_cases(engine, cases[:2], Models(mode="stand_in"))
    assert rows() == before


def test_live_without_a_key_refuses_politely(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    url = os.environ["DATABASE_URL"]
    monkeypatch.setenv("EVAL_DATABASE_URL", url)

    code = harness.main(["--live", "--limit", "1"])

    assert code == harness.EXIT_NO_KEY
    err = capsys.readouterr().err
    assert "nothing was sent or spent" in err
    assert "ANTHROPIC_API_KEY" in err


def test_a_database_not_named_for_evaluation_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/africasignal")
    with pytest.raises(SystemExit) as stop:
        harness.engine_from_env()
    assert "must end in" in str(stop.value)


def test_the_harness_needs_to_be_told_which_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as stop:
        harness.engine_from_env()
    assert "EVAL_DATABASE_URL" in str(stop.value)


def test_record_needs_live(capsys: pytest.CaptureFixture[str]) -> None:
    assert harness.main(["--record", str(Path("x.json"))]) == harness.EXIT_USAGE
