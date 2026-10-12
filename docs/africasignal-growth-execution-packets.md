# AfricaSignal growth execution packets

Date: 2 October 2026. Companion: [detailed implementation plan](africasignal-growth-implementation-plan.md).
For GPT-6 Luna or Gemini 3.8 Flash; same contracts and correctness requirements apply.

## 1. Copyable recipient bootstrap

Provide the actual repository and both documents, then use:

```text
You are implementing AfricaSignal's acquisition, sponsorship and contextual
commercial-intelligence plan. Read applicable AGENTS.md, the detailed plan and
this execution companion. Preserve source permissions, evidence/publication
boundaries, withdrawal, budget accounting, queue fencing and admin security.

Start with F00 ONLY. Refresh origin/main and inspect branch/HEAD, working changes,
source inventory, Alembic head and available CI evidence. Preserve unrelated work
and these handoff documents. Return the baseline and exact F01 context needed.
Do not write application code, install collectors, activate providers/ads, change
prices, send outreach, run live database commands or deploy. Staging is plan-only.

Do not rebuild existing Agent Reach controls, model routing, atomic reservations
or scheduling. Do not copy TV Insights SQLModel/EPG/carriage contracts. Do not
add Gemini as a runtime provider merely because it is your coding model.

For later dispatches implement one supplied task only, then return the actual
diff and check evidence for review. Do not scaffold successors or refactor
unrelated files. Missing signatures, stale hashes or unknown fixture commands
require read-only context retrieval, not guessed code. Stop after each task.
```

## 2. Orchestrator procedure

1. Choose one dependency-ready task. Refresh base/status/file hashes and preserve pre-existing changes.
2. Resolve exact allowed existing and new paths. Task-table path families are not blanket edit authorization.
3. Supply verbatim signatures/imports/DTOs/callers and transaction/error/permission behavior. Label proposed interfaces NEW and finalize them before downstream coding.
4. Supply behavioral acceptance, failure cases and executable checks against a verified isolated environment.
5. Request unified diffs for existing files and additive diffs for new files. Direct tool edits are acceptable with actual resulting diff; do not apply output twice.
6. For supplied patches check hashes and git apply --check. Refresh mismatches; no fuzzy replacement or unnecessary full-file rewriting.
7. Review scope, behavior and meaningful tests. One behavior and a few hundred changed lines is a decomposition guide, not permission to omit required logic.
8. Preserve independent invariant probes. Investigate unexplained failures rather than weakening expectations or increasing skips.
9. Record evidence/status and issue the successor only after review. Narrow repeated failures; use senior review for schema/security/budget/measurement/races.

## 3. Copyable task packet

Fill every placeholder with current evidence. An incomplete packet becomes a retrieval task.

```text
TASK_ID: <one ID and one behavior>
REPOSITORY: ainatunde/AfricaSignal
WORKTREE: <absolute active checkout>
BASE_COMMIT: <actual refreshed SHA>
ALLOWED_EXISTING_FILES: <exact paths>
ALLOWED_NEW_FILES: <exact paths labeled NEW>
FILE_SHA256: <actual hashes>
PRE_EXISTING_CHANGES: <manifest/preservation instructions>
DEPENDENCIES_ACCEPTED: <IDs and evidence>

OUTCOME:
<one observable behavior and purpose>

SCOPE:
Implement only this task. Do not scaffold future work, refactor unrelated files,
change editorial decisions, activate providers/ads, change prices, send outreach,
run live migrations or deploy. Preserve unrelated changes and secrets.

EXISTING CONTRACTS:
<verbatim imports/types/signatures/callers/error behavior, transaction ownership,
settings membership, job admission and relevant source/evidence permissions>

NEW CONTRACTS:
<accepted exact definitions labeled NEW, or NONE>

CURRENT SOURCE:
<smallest complete excerpts with unique anchors/hashes>

ACCEPTANCE:
<positive case plus permission/disable/error/retry/race/staleness cases relevant
 to this task; expected behavior, not a guessed future pass count>

VALIDATION:
<exact commands, resolved disposable DB/storage, fixture behavior and assertions>

EXTERNAL ACCEPTANCE:
<unavailable checks, owner/evidence required; N/A with reason when none>

ROLLBACK:
<task-specific disable/compatibility/data-preservation steps>

OUTPUT:
STATUS: READY_FOR_REVIEW | NEEDS_CONTEXT | FAILED_CHECK
Task ID and base/result commit or diff identity
Actual unified diff and changed-file list
Contract/schema/data/security impact
Actual commands, exit codes and pass/fail/skip counts
Remaining uncertainty, external acceptance and rollback
Stop after this task. Do not execute its successor.
```

## 4. Verified existing interface anchors

Read from b14415a, whose committed tree matches fetched main dfa1655. Refresh at dispatch. These are navigation anchors, not complete executable stubs; include actual implementations/callers before coding.

```python
from africasignal.llm.adapter import build_adapter, LlmAdapter

def build_adapter(session: Session) -> LlmAdapter: ...

# Method on LlmAdapter:
def complete_json(
    self, purpose: str, prompt_version: str, system: str, user: str,
    schema: dict[str, Any], max_tokens: int, *, job_id: int | None = None,
) -> dict[str, Any]: ...

from africasignal.jobs.queue import enqueue

def enqueue(
    session: Session, kind: str, payload: dict[str, Any] | None = None,
    *, dedupe_key: str | None = None, run_at: datetime | None = None,
    max_attempts: int = 5,
) -> int | None: ...

from africasignal.sources.permissions import current_permission

def current_permission(
    session: Session, source_id: int,
) -> SourcePermission | None: ...

from africasignal.operations.agent_reach import TaskSubmission, submit, review_candidate

def submit(
    session: Session, operator: Operator, request: TaskSubmission,
) -> AgentReachTask: ...

def review_candidate(
    session: Session, operator: Operator, candidate_id: int, *,
    action: Literal['accept', 'reject'], source_id: int | None = None,
) -> AgentReachCandidate: ...

from africasignal.operations.workloads import WorkloadChange, configure

def configure(
    session: Session, operator: Operator, name: WorkloadName,
    change: WorkloadChange,
) -> WorkloadControl: ...
```

- TaskSubmission has energy/food topic, query length 8-500, max_results 1-20 and extra-forbidden inputs.
- WorkloadChange contains enabled, WorkloadSchedule and expected_revision. WorkloadName currently allows ai/agent_reach/external_agents/processing only. A new commercial name requires deliberate model/type/policy/control updates.
- WorkloadSchedule has IANA timezone, nonoverlapping weekly windows, max_concurrency and max_items_per_run; its allows check requires aware time. Retain DST/overnight semantics.
- current_permission returns a row or None; presence alone is not an operation authorization decision. Read actual permissions and caller behavior.
- review_candidate accepts eligible active RSS news sources; it is not a generic social importer.
- enqueue uses PostgreSQL JSONB/dedup conflict handling. Read claimed-job token/lease/handler transaction behavior before adding jobs.
- RoutedProvider supports Anthropic/OpenAI with no implicit fallback. Paid purposes call the budgeted adapter, not provider SDKs directly.
- reserve_call in llm/budget.py uses PostgreSQL advisory serialization before network dispatch. Independent accounting sessions preserve billed/uncertain spend across job rollback. Reuse it; do not create a competing commercial AI budget ledger.

| Family | Minimum actual source context |
|---|---|
| A | Runner schemas/backend/store/server/egress, task models/handlers, Reach operations, source permission and evidence persistence/retention callers. |
| B | Models Base/import registry, actual Alembic head, public routes/templates/middleware/cache, admin authorization/CSRF/audit, accepted commercial DTOs. |
| C | Assessment correction/withdrawal/invalidation, LLM adapter/config/budget/schema/cache, queue/policy/handler registry and accepted context DTOs. |
| P | Public security middleware and strict admin CSP, selected-provider current official docs, consent/data map, slot and event contracts. |
| Q | Current CI, integration fixtures, restore/image configuration and readiness registers. |

## 5. First packet: F00

Read-only inspection except reversible Git fetch/reference updates and writing a baseline report. Do not checkout/reset/rebase the current tree during this packet.

Required evidence:

- Exact branch/HEAD, freshly fetched main, divergence and tree diff.
- Working manifest including untracked handoff files and applicable repository instructions.
- Existing AI accounting/scheduling/acquisition/commercial module inventory.
- Current Alembic head/branch topology without live migration.
- Current-main CI evidence or explicit access limitation.
- Safe implementation branch/worktree proposal preserving all current work.
- F01 exact context list and changed assumptions in the plan.

Useful read-only commands, with normal error checking:

```text
git status --short --branch
git fetch origin
git rev-parse HEAD
git rev-parse origin/main
git rev-list --left-right --count HEAD...origin/main
git diff --stat origin/main HEAD
rg --files -g AGENTS.md
rg --files src/africasignal runner tests .github/workflows alembic
```

Inspect migration tooling/interpreter before alembic heads. Never print .env, credentials, secret settings or raw provider tokens.

## 6. Invariant review packets

For schema/security/budget/booking/event changes capture invariants before coding and preserve them after implementation:

- Two workers cannot reserve more than the remaining shared AI allowance.
- Two overlapping exclusive bookings cannot both activate.
- Event replay counts once; forged placement/creative revision fails.
- Withdrawal invalidates context immediately even when rebuild windows are closed.
- Disable blocks queued/manual dispatch; late output cannot publish.
- Unknown/denied/off consent state makes no unauthorized third-party browser request.
- Sponsorship cannot change evidence state/severity/publication/organic ranking.

Run schema/integration/queue probes against dedicated disposable PostGIS. Current fixtures can drop the entire schema; verify TEST_DATABASE_URL and isolated storage before pytest. Browser acceptance needs actual browser evidence, not only rendered HTML.

## 7. Task closeout template

```text
TASK / STATE:
BASE / RESULT COMMIT:
CHANGED FILES:
BEHAVIOR DELIVERED:
CONTRACT / MIGRATION / DATA IMPACT:
CHECKS: command, exit code, result/count/skips
EXTERNAL ACCEPTANCE: qualified / pending / N/A with evidence/reason
RISKS / LIMITATIONS:
ROLLBACK:
NEXT DEPENDENCY-READY TASK:
```

Use the same packet for either model. Verify its tool environment/account access at dispatch; no credentials in prompts. If empirically comparing, run identical narrow pilots against the same baseline in disposable branches, measuring accepted correctness, repairs, latency and actual cost. Do not infer repository performance from vendor benchmarks.

Whole-project completion requires enabled-scope dependencies and the Q acceptance register. A staging plan must never be reported as a deployed pilot.
