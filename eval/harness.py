"""The evaluation harness (AS-040). Run it from the repository root:

    EVAL_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/africasignal_eval \\
        python -m eval.harness

By default it runs offline: no network, no API key. Model answers come from the hand-written
stand-ins in the cases, and every result that depends on them is marked as such. ``--answers FILE``
replays recordings of real model answers; ``--live`` asks the real model (an explicit opt-in that
needs a key). See ``eval/README.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session

from eval.cases import CASES_DIR, Case, CaseSetError, check_set, load_cases
from eval.pipeline import CaseOutcome, Models, Recorder, case_error, run_case
from eval.scoring import CaseScore, Report, evaluate, load_thresholds, score_case

EXIT_OK, EXIT_GATE_FAILED, EXIT_USAGE, EXIT_NO_KEY = 0, 1, 2, 3
SAFE_DATABASE_SUFFIXES = ("_eval", "_test")

NO_KEY_MESSAGE = """\
--live needs an Anthropic API key and none is configured, so nothing was sent or spent.

Set ANTHROPIC_API_KEY in the environment of this command (or save the key in the operator console
Settings of the database named by EVAL_DATABASE_URL), then run again. Without a key the harness
still runs offline on the stand-in answers: leave --live out.
"""

NO_DATABASE_MESSAGE = """\
The harness runs every case in a rolled-back transaction on a PostgreSQL database with PostGIS,
and it needs to be told which one. Set EVAL_DATABASE_URL, for example:

    EVAL_DATABASE_URL=postgresql+psycopg://africasignal:africasignal@localhost:5432/africasignal_eval

The database name must end in _eval or _test. The harness brings its schema up to date (alembic
upgrade head) and stores nothing else: every case is rolled back.
"""


# --- database ----------------------------------------------------------------------------------


def engine_from_env() -> Engine:
    url = os.environ.get("EVAL_DATABASE_URL", "").strip()
    if not url:
        raise SystemExit(NO_DATABASE_MESSAGE)
    name = make_url(url).database or ""
    if not name.endswith(SAFE_DATABASE_SUFFIXES):
        raise SystemExit(
            f"Refusing to use database {name!r}: its name must end in "
            f"{' or '.join(SAFE_DATABASE_SUFFIXES)} so a real database is never touched by mistake."
        )
    # The application's own modules (settings store, migrations) read DATABASE_URL.
    os.environ["DATABASE_URL"] = url
    return create_engine(url, pool_pre_ping=True)


def migrate() -> None:
    from alembic.config import Config

    from alembic import command

    command.upgrade(Config("alembic.ini"), "head")


# --- running -----------------------------------------------------------------------------------


def run_cases(
    engine: Engine,
    cases: Sequence[Case],
    models: Models,
    *,
    progress: bool = False,
) -> tuple[list[CaseOutcome], list[CaseScore]]:
    """Run every case in its own rolled-back transaction and score it. A case that raises is
    recorded as an error, never allowed to stop the run."""
    outcomes: list[CaseOutcome] = []
    scores: list[CaseScore] = []
    for index, case in enumerate(cases, 1):
        started = time.monotonic()
        with engine.connect() as conn:
            transaction = conn.begin()
            session = Session(bind=conn, join_transaction_mode="create_savepoint",
                              expire_on_commit=False)  # fmt: skip
            try:
                outcome = run_case(session, case, models)
            except Exception as exc:  # noqa: BLE001 - a broken case is a result, not a crash
                outcome = case_error(case, exc)
            finally:
                session.close()
                transaction.rollback()
        outcomes.append(outcome)
        score = score_case(case, outcome)
        scores.append(score)
        if progress:
            mark = "ok  " if score.passed else "FAIL"
            print(f"[{index:>3}/{len(cases)}] {mark} {case.id} ({time.monotonic() - started:.1f}s)",
                  file=sys.stderr)  # fmt: skip
    return outcomes, scores


def select_cases(cases: list[Case], args: argparse.Namespace) -> list[Case]:
    chosen = cases
    if args.split:
        chosen = [c for c in chosen if c.split == args.split]
    if args.template:
        chosen = [c for c in chosen if c.template == args.template]
    if args.tag:
        chosen = [c for c in chosen if args.tag in c.tags]
    if args.case:
        wanted = set(args.case)
        chosen = [c for c in chosen if c.id in wanted]
    if args.limit:
        chosen = chosen[: args.limit]
    return chosen


# --- output ------------------------------------------------------------------------------------


def pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:5.1f}%"


def render_text(
    report: Report, scores: list[CaseScore], meta: dict[str, Any], verbose: bool
) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"AfricaSignal evaluation harness, {meta['cases']} cases, mode: {meta['mode']}")
    banner = report.launch
    if not banner["is_launch_measurement"]:
        add("")
        add("NOT A LAUNCH MEASUREMENT. Read every number below as a regression check only:")
        for reason in banner["reasons"]:
            add(f"  - {reason}")
    for template in ("T1", "T2"):
        s = report.summary[template]
        if not s["cases"]:
            continue
        add("")
        add(f"{template}: {s['cases']} cases")
        for key, label in (
            ("decision_accuracy", "decision accuracy"),
            ("evidence_state_accuracy", "evidence-state accuracy"),
            ("severity_accuracy", "severity accuracy"),
            ("place_accuracy", "place accuracy"),
            ("date_accuracy", "date accuracy"),
            ("number_accuracy", "number accuracy"),
            ("claim_validity_accuracy", "claim validity accuracy"),
        ):
            r = s[key]
            add(f"  {label:<26}{pct(r['rate'])}   {r['correct']}/{r['total']}")
        ab = s["abstention"]
        add(f"  {'abstention rate':<26}{pct(ab['rate'])}   {ab['abstained']}/{ab['cases']} "
            f"(labels expect {ab['expected']})")  # fmt: skip
        ex = s["explanations"]
        if ex["asked"]:
            add(f"  explanations asked {ex['asked']}: accepted {ex['accepted']}, after retry "
                f"{ex['accepted_after_retry']}, none {ex['none']}")  # fmt: skip
    add("")
    add("Launch gate (thresholds from eval/thresholds.yaml, taken from the plan):")
    for g in report.gate:
        flag = {True: "PASS", False: "FAIL", None: "n/a "}[g.passed]
        value = pct(g.value) if isinstance(g.value, float) else str(g.value)
        add(f"  {flag}  {g.label}: {value} (threshold {g.threshold})  {g.counts}")
    add(f"  Verdict: {report.verdict.upper()}")
    failing = [s for s in scores if not s.passed]
    add("")
    add(f"{len(failing)} of {len(scores)} cases differ from their labels")
    for s in failing if verbose else failing[:10]:
        add(f"  {s.case_id}")
        for f in s.failures():
            add(f"      {f}")
    if not verbose and len(failing) > 10:
        add(f"  ... {len(failing) - 10} more (use --verbose)")
    return "\n".join(lines)


def result_json(report: Report, scores: list[CaseScore], meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "meta": meta,
        "launch_measurement": report.launch,
        "verdict": report.verdict,
        "gate": [g.__dict__ for g in report.gate],
        "summary": report.summary,
        "cases": [
            {
                "id": s.case_id,
                "template": s.template,
                "split": s.split,
                "gold": s.gold,
                "tags": s.tags,
                "model_answers": s.provenance,
                "passed": s.passed,
                "failures": s.failures(),
            }
            for s in scores
        ],
    }


# --- entry point -------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m eval.harness", description=__doc__.split("\n")[0])
    p.add_argument("--cases", type=Path, default=CASES_DIR, help="directory of *.jsonl case files")
    p.add_argument("--split", choices=["dev", "test"])
    p.add_argument("--template", choices=["T1", "T2"])
    p.add_argument("--tag", help="only cases with this tag")
    p.add_argument("--case", action="append", help="only this case id (repeatable)")
    p.add_argument("--limit", type=int, help="only the first N selected cases")
    p.add_argument("--answers", type=Path, help="replay recorded model answers from this file")
    p.add_argument(
        "--live", action="store_true", help="ask the real model (needs a key, costs money)"
    )
    p.add_argument("--record", type=Path, help="with --live: save the model's replies here")
    p.add_argument("--max-usd", type=float, default=5.0, help="with --live: spending stop")
    p.add_argument("--json", type=Path, help="also write the full result here")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--check-set", action="store_true", help="only validate the case files")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.record and not args.live:
        print("--record only makes sense with --live", file=sys.stderr)
        return EXIT_USAGE

    try:
        every = load_cases(args.cases)
    except CaseSetError as exc:
        print(f"Case files are invalid: {exc}", file=sys.stderr)
        return EXIT_USAGE
    problems = check_set(every)
    if problems:
        print("The reference set is not fit to measure with:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return EXIT_USAGE
    if args.check_set:
        print(f"{len(every)} cases, no problems")
        return EXIT_OK
    cases = select_cases(every, args)
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return EXIT_USAGE

    engine = engine_from_env()
    migrate()

    models = Models(mode="stand_in")
    if args.answers:
        models = Models(mode="recorded", recorded=json.loads(args.answers.read_text("utf-8")))
    if args.live:
        from africasignal.llm import LlmNotConfigured
        from africasignal.llm.adapter import make_provider

        with Session(engine) as probe:
            try:
                make_provider(probe)
            except LlmNotConfigured:
                print(NO_KEY_MESSAGE, file=sys.stderr)
                return EXIT_NO_KEY
        recorder = Recorder() if args.record else None
        models = Models(mode="live", live_provider=make_provider, recorder=recorder,
                        max_usd=args.max_usd, explain_all=True)  # fmt: skip
        print(f"Live run: up to ${args.max_usd:.2f} of model calls, {len(cases)} cases.",
              file=sys.stderr)  # fmt: skip

    outcomes, scores = run_cases(engine, cases, models, progress=True)
    report = evaluate(cases, scores, load_thresholds())
    meta: dict[str, Any] = {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cases": len(cases),
        "mode": models.mode,
        "model_answers": dict(Counter(s.provenance for s in scores)),
        "gold": dict(Counter(s.gold for s in scores)),
        "model_calls": sum(o.model_calls for o in outcomes),
    }
    print(render_text(report, scores, meta, args.verbose))
    if args.json:
        args.json.write_text(json.dumps(result_json(report, scores, meta), indent=2, default=str))
    if args.record and models.recorder is not None:
        args.record.write_text(
            json.dumps(models.recorder.to_json({"recorded_at": meta["run_at"]}), indent=2)
        )
        print(f"Recorded replies saved to {args.record}", file=sys.stderr)
    return EXIT_GATE_FAILED if report.verdict == "fail" else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
