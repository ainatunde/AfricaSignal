# AfricaSignal autonomy implementation baseline

Recorded 14 October 2026. This is a source and local-checkout record. It does not establish a
deployed runtime, provider qualification, editorial acceptance, or authority to publish externally.

## Pinned source and runtime boundary

- GitHub origin/main was refreshed before implementation and resolved to
  56abf1e70c674776cb8f13a4bbde23938c1f5dbf (PR #35 merge). The source checkout was clean.
- Implementation is isolated on codex/africasignal-autonomy-implementation from that exact
  commit. Changes are local and uncommitted; main was not modified.
- The repository contains a scheduler, worker, durable queue, recovery fences and source adapters
  for NBS, NERC, price announcements, RSS and GDELT. These are code capabilities, not proof that a
  deployed service is running them or that an operator has approved each source.
- sources.yaml seeds source permissions unapproved. The live database's approved-source set,
  service supervision, credentials, notifications, backups and deployment SHA were unavailable.

## Reproduced correctness gaps and A00-A04 source changes

The audit probe was rerun against the pinned source before edits:

- An explanation reversed an explicit source negation and passed validation.
- “Petrol costs have made every household poorer” passed despite no household-level evidence.
- An extracted petrol claim marked up with NGN/kg passed although its quote said the price fell per
  litre.

The claim extractor now uses claim_extract_v2 and claim_validate_v2. Numeric claims bind the value,
currency and unit locally to the quote and catalogue; contradictory values or directions are
rejected. Unclear direction is represented as unknown. Explanation generation uses the
explain_v3+grounding_v3 contract; assertions reference typed facts or claims and deterministic
validation checks polarity, qualifier and scope. The held-out regression set has 120 cases, 40
adversarial, and is a semantic regression set rather than editorial acceptance. Historical claims
still need a durable revalidation/backfill.

AS-A03 adds one explainable publication-eligibility decision shared by website, email, social and
replies. It checks current version state, evidence and claim validity, supersession, current numeric
reuse permission, and captured/current retention. Permission reductions suppress public reads,
cancel queued work and enqueue reassessment without rewriting source records. Public responses use
no-store and the situation cache includes the permission signature.

AS-A04 adds freshness, scheduler, queue, delivery, budget, storage and provider-state probes to the
operator alert path. Storage probes are non-mutating. AS-A05 adds one delayed, permission-checked
retry for recognized replay-safe source transport failures and checks whether it recovered. The
probe and repair are scheduled through the application's own scheduler and database; an independent
external heartbeat and alert recipient are not configured. Parser, rights and ambiguous-delivery
failures are not automatically repaired.

## AS-A05 through AS-A10 implementation state

AS-A05 is partial. Deterministic operational probes and a single bounded source retry are present.
The repository already contains an HTTPS external-heartbeat client wired into the worker and
scheduler. No independent monitoring URLs or alert delivery target are configured or live-tested
here, so independent supervision is not qualified. Guardian probes still run through the
database-backed scheduler; backup and recovery qualification also remain open.

AS-A06 adds deterministic private opportunity packets for material changes, comparable price
changes, short persistent trends and evidence disagreement. Current eligibility and evidence IDs
are required; every fact must point to an eligible evidence document. Selection is not publication
authority.

AS-A07 adds an operator review page for those packets and the existing private, model-generated
editorial drafts. The packets show scope, period, freshness, unknowns, sources and evidence links.
Candidate review records 1–5 usefulness, correct scope and critical unsupported assertions, and
shows a live sample against the proposed 90% useful-and-scoped / zero-critical target. No editorial
sample or held-out brief evaluation has been completed; the 120-case semantic regression set does
not measure brief usefulness or social-reply quality.

AS-A08 adds exact content and destination hashes, authorization snapshots, assessment dependencies,
provider receipts, a durable pre-send fence and explicit sent/simulated/rejected/unknown outcomes.
Email timeouts and other ambiguous outcomes are not retried automatically. “Sent” means the provider
accepted the request; it does not prove mailbox delivery. Invalidating evidence or reuse permission
creates channel-specific delivery-correction work for confirmed prior sends. An operator can mark
work fulfilled only against a later confirmed receipt for the same channel and recipient/account,
linked to the current eligible assessment. This does not edit or remove old posts automatically,
and no provider read-back/edit/delete behavior has been qualified.

AS-A09 adds an account-bound X mentions inbox with a bounded paginated reader, dedupe, self-loop
suppression, 30-day retention for unlinked mentions, target availability checks and ID-only storage.
It discards mention text and profile data; an operator opens the original X item and classifies it.
Only a dated fact or source request can produce a deterministic reply draft, linked to a current
eligible fact packet and evidence URL. Clarification, correction, unsupported, out-of-scope,
abuse/spam and other input are escalation categories. Replies require an individual human review,
account-token identity check, current evidence/permission recheck, conversation cap and daily limit.

AS-A10 now has a revision-1 policy record seeded in `draft` state: Nigeria food/energy, dated-fact
and source requests only, one reply per conversation, at most ten replies per Lagos day, with no
mention-text retention. Policy evaluation must record passing coverage for ambiguous scope/time,
contradictory evidence, stale evidence, correction reports, malicious instructions, repeated mentions
and bot loops, with zero critical errors. A different operator must approve the evaluation; staging
evidence and another operator are required to activate, and activation is limited to `ENV=staging`.
After activation, each reply still requires operator classification, current assessment selection
and exact-response review. Dispatch rechecks the policy revision, quota, conversation, target,
current evidence and permissions, and records the policy authorization with the X receipt.

This does not yet provide autonomous A4 answering. The current reply text is a generic broadcast
post rather than a question-aware answer, and the inbox discards the question text. No accepted
conversation evaluation report, editorial approval or staging sign-off has been recorded; the seeded
policy remains inactive. Social posting and replying remain per-item reviewed.

## Local verification and remaining gates

The local unit suite and static checks are recorded in the dated handoff. PostgreSQL integration
tests, including the policy-transition test, were not run because DATABASE_URL is unset and the
integration harness requires a disposable database ending in `_test` before it drops and recreates
a schema. No live source, social account, email provider, staging host or production runtime was
accessed. Official X API docs support the mentions read and reply endpoints, but the
configured account's entitlement, credentials, quotas, pricing and live delivery were not tested:
[mentions endpoint](https://docs.x.com/x-api/users/get-mentions), [post and reply endpoint](https://docs.x.com/x-api/posts/manage-tweets/introduction).

AS-A11 remains open. It needs the named editorial, operating and release owners; approved source and
distribution policy; a staging host and safe database; authorized provider configuration; real
provider receipts; failure, correction, suspension and recovery drills; and separate editorial
acceptance. No migration was applied and nothing was deployed or published.
