# AfricaSignal autonomy implementation handoff

Recorded 14 October 2026. The implementation is reviewable in an isolated, uncommitted worktree.
No migration was applied; no provider send, public post, deployment or push was performed.

## Source and task state

- Base commit: 56abf1e70c674776cb8f13a4bbde23938c1f5dbf, the refreshed origin/main PR #35 merge.
- Branch: codex/africasignal-autonomy-implementation.
- Worktree: C:\Users\aina_\.gemini\antigravity\scratch\AfricaSignal-autonomy-implementation.
- The original checkout and origin/main were left unchanged. The worktree contains local source,
  test and documentation changes only.

## Implementation packets

- AS-A00-A04: reproduced claim/explanation failures; added stricter claim and explanation
  grounding, shared current publication eligibility, permission suppression, freshness and
  operational alerts, storage probes and bounded replay-safe source repair.
- AS-A05: added a durable repair attempt and outcome recheck for one recognized transient source
  fetch failure per source generation. The existing HTTPS external-heartbeat client is wired into
  worker and scheduler, but no independent monitoring URLs or alert delivery target are configured
  or live-qualified here. Guardian probes still run through the database-backed scheduler.
- AS-A06-A07: added deterministic evidence-linked opportunity packets and a private admin review
  page. Candidate review now records 1–5 usefulness, scope correctness and critical unsupported
  assertions, with live sample metrics against the proposed 90%/zero-critical target. No editorial
  sample or held-out usefulness evaluation has been completed. Selection never publishes or
  distributes a candidate.
- AS-A08: added per-attempt email/social/reply authorization and receipt records, content/destination
  hashes, pre-send fences, simulated versus provider-accepted states, and no-blind-retry handling
  for uncertain outcomes. Invalidation creates correction work; an operator can mark it fulfilled
  only against a later confirmed same-channel/same-target receipt for the current eligible version.
  This does not establish mailbox delivery or provider post edit/delete/read-back behavior.
- AS-A09: added X mentions polling with account binding, pagination, dedupe, ID-only retention,
  expiry, target checks, fact-grounded reply drafts, explicit request categories and operator triage.
  Only dated-fact and source requests can draft; clarification, correction, unsupported, out-of-scope,
  abuse/spam and other categories are escalated. The inbox stores no mention text and does not
  classify or answer arbitrary social content.
- AS-A10: added a revision-1 X reply policy seeded as `draft` and disabled: Nigeria food/energy,
  dated facts/source requests only, one reply per conversation, at most ten per Lagos day, and no
  retained mention text. Recorded evaluation must cover seven conversation-risk families with no
  failed cases or critical errors; a different operator must approve, and another operator must
  attach staging evidence and activate from `ENV=staging`. Every reply still requires operator
  classification, assessment selection and exact-response review. The dispatcher rechecks policy,
  thread, quota, target, current evidence, account and permissions, and records the policy revision
  with the provider receipt. This is bounded human-reviewed operation, not autonomous A4 answering:
  the current generic broadcast post is not question-aware, and no accepted conversation evaluation
  report has been supplied.
- AS-A11: not qualified. Staging, approved providers/accounts, named editorial/operating/release
  owners, policy approval, live receipts, rollback and correction drills are not available here.

## Files and migrations

The implementation touches claim/explanation extraction and assessment, publication eligibility,
public query suppression, Guardian/health/storage checks, candidate selection, email and social
dispatch, X API access, reply operations, correction work, admin routes/templates, and their tests.
The local migrations are:

- 0065: explanation assertion provenance.
- 0066: publication eligibility provenance.
- 0067: bounded Guardian source-repair attempts.
- 0068: deterministic insight candidates.
- 0069: X mention/reply records and delivery attempt receipts.
- 0070: delivered-item correction work ledger.
- 0071: inactive bounded X reply-policy draft and candidate usefulness/scope ratings.

Alembic reports one head, 0071. These migrations have not been applied to PostgreSQL.

## Validation

- Ruff check src tests: passed.
- Ruff format check src tests: passed; 394 Python files already formatted.
- Mypy src/africasignal: passed, 218 source files.
- Unit suite: 1,236 passed, 4 skipped.
- Both changed admin templates compiled with Jinja.
- Ruff check and format check for migration 0071 passed; Alembic reports one head, 0071.
- git diff --check: passed.
- PostgreSQL integration tests were skipped. DATABASE_URL is unset, and the integration harness
  requires ENV=development and a dedicated database ending in _test before it drops and recreates
  a schema. An unscoped `pytest -q` hit database-backed fixtures without that target and was stopped;
  the explicit unit suite passed. The new policy transition, migration execution, concurrent
  send/permission races, mention service journey and correction workflow remain unverified against
  PostgreSQL.

## Runtime and product gates

Official X documentation describes the [account mentions endpoint](https://docs.x.com/x-api/users/get-mentions)
and [post/reply endpoint](https://docs.x.com/x-api/posts/manage-tweets/introduction). The X account's
entitlement, credentials, quotas, cost, target availability and live delivery were not tested.
Email provider adapters remain mock-tested only. No live source collection, operator notification,
social send, email send, staging run or production runtime was accessed.

Next ownership is required for a safe pilot: a named release/editorial/operating owner, approved
source and distribution/reply policy, staging host and disposable integration database, and
authorized provider configuration. After those exist, run migrations and PostgreSQL integration
tests, qualify one X account and email provider, test the correction workflow, then perform staged
reviewed operation and rollback drills. The seeded policy remains disabled; the current generic
post is not a question-aware answer, so replies must continue to receive exact per-item review.
