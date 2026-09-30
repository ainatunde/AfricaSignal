# Evaluation harness (AS-040)

The harness runs the assessment pipeline on a reference set of cases and compares every result with
a label. It reports, per template (T1 price change, T2 policy change):

- decision accuracy (publish, hold, withhold, publish as insufficient, nothing to assess),
- evidence-state accuracy (reported, corroborated, disputed, insufficient),
- place accuracy and date accuracy,
- number accuracy (the plan requires 100 %),
- abstention rate, next to the number the labels expect,
- counts behind every rate.

It also checks the launch gate from the plan and prints PASS or FAIL for each line. The numbers are in
`thresholds.yaml` and are the plan's, not the build's:

| Check | Threshold |
| --- | --- |
| Number accuracy | 100 % |
| Published assessments with a place finer than their evidence | 0 |
| Evidence-state accuracy, T1 | at least 90 % |
| Evidence-state accuracy, T2 | at least 80 % |

## Read this first: what a result means today

**Every result from this repository's reference set is a regression check, not a quality
measurement.** Two things are still missing, and the report says so in a banner whenever they are:

1. **The model's answers are stand-ins.** No API key exists yet, so the answer the language model
   gives in each case is hand-written in the case file. The harness runs the real extraction code
   (validation, place resolution, corroboration) on those answers, but nothing here says how well a
   real model reads real documents. Results are marked `stand_in`.
2. **The labels are synthetic.** `seed.py` writes them, and computes each expected number and state
   from the rules in the plan, independently of the code under test. They are a good check that the
   pipeline does what the plan says. They are not an editor's judgement of what is right. Cases are
   marked `"gold": "synthetic"`.

A run counts as a launch measurement only when the labels come from the editorial owner
(`"gold": "editorial"`), the answers are a real model's (`--live`, or a recording made with it), and
the set has at least 60 T1 and 40 T2 cases. Until then, a PASS here means "the code still does what the plan
says on these cases", not "the product is good enough to launch".

## Running it

The harness needs PostgreSQL with PostGIS, like the tests. Point it at a database whose name ends in
`_eval` or `_test`; it refuses anything else so a real database is never touched. It brings the schema
up to date and runs every case in a transaction that is rolled back, so nothing is left behind.

```sh
EVAL_DATABASE_URL=postgresql+psycopg://africasignal:africasignal@localhost:5432/africasignal_eval \
  python -m eval.harness            # all cases, offline, stand-in answers
```

Useful options: `--template T1|T2`, `--split dev|test`, `--tag stale`, `--case t1-…`,
`--limit N`, `--verbose` (every disagreement), `--json result.json`, `--check-set` (only validate the
case files). The exit code is 0 for pass, 1 when the gate fails, 2 for a usage problem, 3 when
`--live` was asked for without a key. A run with an incomplete gate (for example one template only)
exits 0 and says `INCOMPLETE`.

The harness runs in CI as part of `pytest` (`tests/integration/test_eval_harness.py`), on a subset of
cases that covers every kind in the plan. The whole set takes under a minute.

### Model answers: three modes

| Mode | How | Marked |
| --- | --- | --- |
| Stand-in | default; the answers written in the case files | `stand_in` |
| Recorded | `--answers replies.json`; replays real replies saved by an earlier `--live --record` run | `recorded` |
| Live | `--live`; asks the real model through the application's own adapter | `live` |

`--live` is an explicit opt-in. Without a key (the `ANTHROPIC_API_KEY` environment variable, or the key
saved in the operator console Settings of the evaluation database) it prints a short explanation, sends
nothing, spends nothing and exits with code 3. With a key, `--max-usd` (default 5) stops the spending.
`--record FILE` saves the replies so the same run can be replayed offline and compared after a
change.

## The reference set

`cases/*.jsonl`, one case per line (blank lines and lines starting with `#` are ignored). Do not edit
the generated files by hand: change `seed.py` and run `python -m eval.seed`; a unit test fails when
the files are out of date. To add cases labelled by a person, put them in a separate file (for example
`cases/t1_editorial.jsonl`) with `"gold": "editorial"`; the generator never touches it.

A case holds the inputs (documents with source, date and text; measurements for the NBS series; the
model's stand-in answer for each document), a situation (an item and place for T1, a policy series
for T2) and the labels: decision, evidence state, severity, place, period, the numbers that must
appear, the rules that must fire, and the validity of each claim. `cases.py` documents the schema.

The seed set has 102 cases (60 T1, 42 T2). Besides ordinary price moves and boundary values
for materiality and severity, it includes the situations the plan names:

- stories copied from one wire report and reprinted (`copied_wire`), and official documents
  reprinted by the press (`copied_official`),
- stale data (`stale`), a missing effective date (`unknown_date`),
- ambiguous place names (`ambiguous_place`), for example a name shared by two places,
- a GDELT row about Niger that must not be taken for Nigeria, and the reverse (`niger_vs_nigeria`),
- NBS numbers that were revised after publication (`revision`),
- a policy that was later reversed or suspended (`policy_reversed`),
- a claim whose passage is not in the document, or whose value is not in its passage
  (`hallucination`),
- withdrawn documents, the kill switch (R1), the review queue (R4), no primary document (R5) and
  the hold on a first high-severity assessment (R7).

### Splits

Every case belongs to a `dev` or `test` split. Tune on `dev`, and look at `test` only to check. Cases
about the same reporting origin (a wire story and its reprints, or one regulator's orders for one
series) share an `origin_group` and so always land in the same split; `check_set` fails if a group
spans both splits, and also if two near-duplicate documents (the same SimHash test the application
uses) sit in different splits. Any run starts with these checks.

## Sources and data

- `places/` is a small cut of the Nigerian administrative boundaries, used to resolve place
  names without the network. geoBoundaries gbOpen, CC BY 4.0, attribution: William & Mary geoLab.
  The same cut is in `tests/fixtures/places`.
- The documents in the cases are written for the set. They do not contain text from any news site,
  regulator or statistics office, which matters because this repository is public.
- Real recordings made with `--live --record` will contain real source text. Do not commit them
  without checking each source's terms.

## Files

| File | What it does |
| --- | --- |
| `harness.py` | the command line, database safety, running and printing |
| `pipeline.py` | runs one case through the real application code |
| `scoring.py` | compares results with labels and applies the gate |
| `cases.py` | the case schema and the checks that keep the set honest |
| `seed.py` | generates the synthetic seed set |
| `sources.py` | the sources cases may name |
| `thresholds.yaml` | the launch gate, from the plan |
