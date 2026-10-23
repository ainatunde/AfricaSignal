# Research and correction implementation record

Baseline: `8eeafe432e9adf8ff2175ea89c180d4e0c7a653d`, isolated worktree
`AfricaSignal-research-correction`, branch `codex/autonomous-research-correction`.
Refreshed main was `7b14576089de4e0450012c0b91909a2cbbbc71f7`; baseline includes
`ccb0049` and `8eeafe4` visitor-check/fixture changes. Alembic head is `0072`.
Original checkout's two untracked planning documents remain untouched.

AS-R00 / AS-R07 first checkpoint: implemented revisioned policy, off-by-default
authority, fail-closed canary qualification and disabled/blocked Guardian semantics.
Existing processing workload controls bound research scheduling; source scope is
explicit. External send authority cannot be enabled by research policy.
Unit checks cover invalid activation, duplicate evaluation cases, unsafe outcomes,
critical errors and synthetic-versus-editorial evidence. Live editorial and
runtime acceptance are not asserted. Engineering owner for this implementation:
current Codex session; actual editorial/operating owners remain unassigned.

Local implementation acceptance checkpoint: 81 focused unit/PostgreSQL integration
checks pass; repository Ruff checks and formatting pass; `mypy src` passes across
233 source files. PostgreSQL upgraded from base through `0073`, and downgrade to
`0072` followed by re-upgrade to `0073` succeeded. At the checkpoint, `0073` and
all four research lineage tables were present; the disposable database container
was removed after validation. Alembic's global `check` reports
pre-existing PostGIS Tiger table and model constraint-name drift; it listed no
research-table drift. The unrelated `test_streams_wired` subprocess checks use the
original checkout's virtual environment and failed in this Windows setup; the
admin security probe also uses POSIX-only `SIGALRM`. These are environment limits,
not passing release evidence.

Implementation checkpoint: migration `0073` adds durable questions, per-request
attempts, one validated decision per generation and one correction action. Existing
approved NBS/NERC adapters own factual writes, constrained to the question's item,
place or policy series. Research requests are restricted to currently approved
HTTPS source hosts and reserve global request budget before each fetch. The admin
API exposes status, redacted question lineage, revisioned policy changes and
immutable reviewer-attributed qualification records. Public corrections are checked
against the actual situation and history pages; research-created versions cannot
trigger follower distribution hooks.

Guardian checks handler/schema readiness, planner freshness, stalled questions and
pending public correction verification. Ordinary findings require two failed
observations to open and two successful observations to resolve. Canary activation
requires the target policy revision and exact approved source scope plus the
pre-canary gates; the seven-day canary remains a separate promotion gate. The
existing external heartbeat wiring is available, but an actual monitor receipt,
resource headroom, staging browser runs and owner sign-off are external acceptance
evidence and remain unverified.

Subsequent phases may be built and regression-tested while release gates remain
closed. No pilot is activated until external acceptance exists. This distinguishes
implementation acceptance from pilot acceptance; unavailable live evidence is not
replaced by a passing unit test.
