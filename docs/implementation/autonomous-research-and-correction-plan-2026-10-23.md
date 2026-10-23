# AfricaSignal: autonomous research and correction

Date: 23 October 2026. Status: proposed implementation plan; no application changes, migrations, provider calls, publication, or deployment performed for this plan.

## Outcome and boundaries

Give AfricaSignal the operational loop already represented in TV Insights: detect missing or doubtful information, research appropriate sources, store an evidence-backed decision, apply a permitted correction, recheck the public result, and escalate only when the bounded process cannot resolve it.

Parity means comparable research and correction outcomes, not copying TV Insights' media-specific tables or every automated decision. AfricaSignal must continue to distinguish verified measurements, reported claims, interpretation, and unknowns. A model may judge meaning against evidence; it may not invent measurements, infer causation from news proximity, approve source rights, change publication policy, execute arbitrary commands, or authorize external distribution.

The initial pilot is the existing Nigeria food/energy T1 price-change and T2 policy-change scope. Research, validation and eligible website corrections should run without per-item operator intervention after pilot activation. Human review remains for new source permissions, ambiguous factual changes and external correction sends. Fully automatic social conversation and unrestricted publishing are separate work.

## Verified baseline

Inspected checkout: `C:/Users/aina_/.gemini/antigravity/scratch/AfricaSignal`, branch `codex/priority-transfers`, HEAD `8eeafe432e9adf8ff2175ea89c180d4e0c7a653d`. Refreshed `origin/main`: `7b14576089de4e0450012c0b91909a2cbbbc71f7`. The checkout is two commits ahead; its visitor-page checks and fixture changes are not on that main reference. The existing untracked `docs/implementation/africasignal-2.0-crowd-pricing-plan.md` is unrelated and must be preserved.

TV Insights reference: checkout HEAD `a4beb9d160295773682324eb68ffda2f707dfef0`; refreshed main `c890fbf6ede5d616535a9ab556b52f21a289ec53`. Its checkout has unrelated modifications. This is source inspection, not live-system qualification.

Paths below are relative to the AfricaSignal repository unless prefixed with `tv-insights/`. Existing code inspected:

| Foundation | Existing implementation | Implication |
| --- | --- | --- |
| Scheduled durable work | `src/africasignal/jobs/scheduler.py`, `jobs/queue.py`, `jobs/execution.py`, `jobs/policy.py` | Reuse dedupe, leases, cancellation/effect fences and workload schedules; do not add a second scheduler or queue. |
| Collection and evidence | `sources/{nbs,nerc,price_announcements,rss,gdelt}.py`, `evidence/capture.py`, `net/fetch.py`, `sources/permissions.py` | Reuse approved adapters, safe fetch, robots/rate limits, capture hashes and retention. GDELT discovers URLs; it is not evidence by itself. |
| Independent reporting | `evidence/origins.py`, `assess/corroboration.py` | Reuse reporting-origin handling; duplicate URLs or syndicated reports cannot be counted as independent corroboration. |
| Assessments and interpretation | `jobs/handlers/assess_situation.py`, `publish/situations.py`, `assess/{price_change,policy_change,explain,retrieval,editorial}.py` | Facts and publication policy already run separately from optional model explanations and private drafts. Retrieval is assessment-scoped. |
| Insight selection | `assess/insight_candidates.py` | Existing candidates cover material changes, disagreement, persistent trends and comparisons. Extend this service rather than creating a competing insight engine. |
| Corrections | `publish/invalidation.py`, `assess/invalidation.py`, `publish/versions.py`, `publish/eligibility.py`, `operations/delivery_corrections.py` | Invalidated inputs already trigger reassessment/withdrawal, version history, pending-send cancellation and correction work. The missing capability is broader automatic detection and verified completion. |
| Guardian | `health_alerts.py`, `backup_alerts.py`, `guardian/remediation.py`, `jobs/handlers/check_health.py` | Existing operating alerts and one extra delayed retry for recognized transient source-fetch failures. Parser, permission and delivery failures are not repaired automatically. |
| Visitor checks, branch only | `guardian/public_pages.py`, `jobs/handlers/check_public_pages.py`, migration `0072_guardian_public_page_checks.py` | Reconcile/release this prerequisite explicitly before depending on persisted public observations. |
| Discovery runners | `operations/agent_reach.py`, `jobs/handlers/agent_reach.py`, `operations/external_agents.py` | Agent Reach currently qualifies only fixed YouTube metadata search and reviewed candidate intake. Generic external agents produce untrusted outputs. Neither is a verified general-web research engine. |
| External communication | `publish/social.py`, `operations/social_replies.py`, `operations/social_reply_policy.py`, `models/ops.py` | Preserve exact review, destination/evidence checks, receipts and uncertain-outcome handling. The X reply policy does not grant arbitrary autonomous answering. |
| Independent process monitoring | `ops_heartbeat.py` | Heartbeat hooks exist; monitor configuration, alert delivery and dead/hung-process detection require live qualification. |

TV Insights patterns to adapt: `app/platforms/research.py` for evidence-led questions and remembered attempts; `app/judge/stories.py` for bounded semantic decisions with recorded methods; `app/autonomy/run.py` for controlled, reversible actions; `app/guardian/selfheal.py` for queued repairs and cooldowns. An enqueued job is not proof of recovery.

The checked-in production acceptance record is historical and contains unresolved gates. This plan does not refresh or override it. No current staging runtime, source approval register, provider receipts, notification endpoint or resource envelope was inspected.

## Proposed architecture

`Guardian finding / assessment unknown / changed source -> ResearchQuestion -> queued approved-source research -> captured evidence -> validated decision -> existing reassessment/invalidation -> public observation -> resolved or escalated`

Add a small `research/` package and additive records, while keeping authoritative facts in existing evidence, measurement, claim and assessment tables:

| Proposed record | Responsibility |
| --- | --- |
| `ResearchQuestion` | Typed problem, exact topic/item/place/period, trigger evidence and affected versions, priority, deadline, policy revision, generation, next due time and terminal reason. |
| `ResearchAttempt` | Question generation, job/lease identity, source permission snapshot, request/result references, expenditure, retries and next retry time. Store permitted metadata, never unrestricted scraped text. |
| `ResearchDecision` | Closed verdict, supporting/contradicting evidence IDs, method/model/prompt/version, scope, reason and validation outcome; no direct fact mutation. |
| `CorrectionAction` | Approved action class, before/after version IDs, triggering decision, dedupe identity and independent outcome verification. Reference existing `DeliveryCorrectionWork`; do not duplicate its receipt ledger. |

Suggested question lifecycle: `open -> queued -> researching -> evidence_ready -> validated -> applying -> verifying -> resolved`; explicit alternatives are `waiting_permission`, `waiting_budget`, `waiting_window`, `unresolved`, `escalated`, `cancelled`. A transport failure is not a negative factual answer. A stale generation must not overwrite newer evidence or decisions.

Record a new generation only for materially changed inputs, source permissions, scope or policy. Dedupe by question type + exact entity/place/period + evidence generation + policy revision. Repeated probes update the same question and incident, not new rows indefinitely.

## Delivery sequence

### AS-R00 — Reconcile the baseline and define authority

Implementation: compare the two AfricaSignal commits with main, reconcile the branch-only visitor checks, verify the actual Alembic graph/head on the chosen implementation baseline, and create a dedicated branch/worktree. Allocate migration revisions after checking the current graph; do not assume `0073` is free. Add a versioned autonomy policy through existing settings/workload controls, with separate research, decision, automatic application and external-send controls. Default new capability off.

Acceptance:

- Baseline SHA, schema target, affected files, source/topic scope, accountable engineering/editorial/operating roles and policy revision are recorded. Roles may be proposed until people are assigned; pilot activation requires actual owners.
- Existing visitor checks are either included in the release or explicitly unavailable; mainline and branch capability are not conflated.
- Disabled workloads enqueue no research and report `not_scheduled`; enabled but unqualified capability reports blocked/unknown with a reason.
- Publication policy `pp-1`, rights and existing holds remain authoritative. No automatic drafting or correction creates external-send authority.

Guardian: add `autonomy_configuration` and `research_schedule`. Detect missing handlers, incompatible schema, inconsistent flags and enabled workloads that never run. Fail closed on application, then escalate; do not self-enable features.

### AS-R01 — Detect researchable gaps and persist questions

Implementation: add `research/questions.py`, `research/planner.py` and a bounded scheduled `plan_research` handler. Read current assessment unknowns, missing primary evidence, stale reference periods, explicit evidence disagreement, failed processing, and sampled public mismatches. Extend candidate refresh to link to questions. Compare freshness to expected source release/validity periods; a recent successful fetch does not make an old measurement current.

Acceptance:

- Each supported trigger creates one scoped question with the affected version, evidence, reason and deadline; unsupported topics become explicit unresolved cases.
- Duplicate scheduler ticks and concurrent planners create one active question per generation.
- A later generation cancels or supersedes obsolete work. Backlogs have bounded scans, indexes, retention and priority aging so low-priority questions cannot starve indefinitely.
- A missing fact stays unknown on the website until validated; a gap never creates an invented measurement or a stronger evidence state.

Guardian: `research_backlog`, `research_coverage`, `research_stalled`. Watch oldest eligible question age, missing expected questions and no-progress attempts. Requeue only expired replay-safe work through existing lease recovery; escalate exhausted attempts.

### AS-R02 — Research approved sources automatically

Implementation: add `research/sources.py` and a `research_question` handler. Start with configured first-party NBS/NERC/company sources and their existing adapters. For approved news domains, use existing RSS/GDELT discovery and capture. Build typed queries from the question scope rather than allowing unconstrained model browsing. Candidate URLs go through `process_document`, source identity checks, permission review, safe fetch and evidence capture. Same-domain and redirect checks must bind final content to an approved source; public reachability alone does not confer permission.

Unapproved domains may become metadata-only review leads where the configured discovery service permits that metadata. Do not fetch their content or create rights approvals automatically. A general search provider or broader runner is optional later work with a separate vendor/rights/cost decision; it is not a prerequisite for the first approved-source pilot.

Acceptance:

- A scoped missing-primary-document question can automatically find and process an available document on an approved source, and link its evidence to the originating question without operator URL entry.
- Changed content at one URL yields a distinct capture. Unchanged content creates no duplicate evidence or correction.
- Permission revocation, review expiry, forbidden redirects, robots refusal, exhausted budgets and timeouts yield distinct recorded outcomes, with current permission rechecked immediately before capture/processing.
- Malicious source instructions cannot alter tasks, model policy, credentials or destinations. Bytes, redirects, request counts and execution time are bounded; a killed worker resumes safely through the existing queue.

Guardian: `research_source_access`, `research_evidence_capture`, `research_budget`. Retry recognized transport failures within a durable question budget; respect rate-limit backoff; escalate parser drift and permissions. Never bypass robots or silently substitute an unapproved source.

### AS-R03 — Validate meaning and produce auditable decisions

Implementation: add `research/judge.py` and `research/validation.py`, using the existing LLM adapter, purpose routing, cache and spend reservations. Deterministic checks bind exact numbers, units, currency, geography, population, reference period and effective dates. A bounded model judges semantic relevance, explicit contradiction, support and scope using captured passages and a closed result schema. Use verdicts such as `supported`, `contradicted`, `insufficient`, `out_of_scope`; recheck all cited IDs and rights in code. A model's confidence score never grants authority.

Preserve reporting origins and investigate questionable attribution; similarity is a candidate signal, not proof of independent reporting. Official-source identity is necessary but does not make different document types, populations or periods comparable. Unknown provenance cannot be promoted to independent corroboration.

Acceptance:

- A current authoritative revision for the same fact can validate a replacement; different periods, populations or units cannot silently replace it.
- The evaluator catches negation reversal, causation without evidence, future-versus-effective dates, geographic overreach, syndicated copies, stale evidence and source prompt injection.
- No model/key/budget leaves semantic questions unresolved. Previously eligible facts remain available; invalid outputs are suppressed independently. Model fallback is recorded and never presented as semantic validation.
- Every decision records evidence, method and validator/prompt/model/policy versions. Citation validity alone is insufficient: held-out semantic correctness is measured in AS-R07.

Guardian: `research_grounding`, `research_scope`, `research_origin_integrity`, `research_model_availability`. Quarantine rejected decisions, rerun once only if genuinely transient, and escalate recurrent contradictions or validation failures. No live Guardian probe may invoke unlimited model work.

### AS-R04 — Apply eligible corrections through the existing pipeline

Implementation: add a typed action dispatcher in `research/corrections.py`. Reuse `publish/invalidation.py`, `assess_situation`, `publish/versions.py`, `publish/eligibility.py` and the current assessment algorithms. Capture source revisions first; never overwrite the old measurement or assessment to match model prose. Enforce current generation and policy immediately before committing an action. Extend explanation invalidation so stale/unsupported explanations disappear while valid underlying facts can remain.

| Finding | Permitted automatic action | Review boundary |
| --- | --- | --- |
| Validated authoritative restatement of the exact fact | Existing adapter supersession, invalidation and reassessment; publish only if current policy permits. | Ambiguous source authority, scope or mapping escalates. |
| Current reuse rights removed or retention expired | Suppress affected content, cancel pending delivery and queue reassessment/correction work. | Never self-grant rights; pausing collection alone must not erase still-valid reuse rights. |
| Proven unsupported explanation | Suppress that explanation and queue bounded regeneration from current valid facts. | Unresolved interpretation remains absent/unknown. |
| Newly found contradicting report | Reassess evidence state and show dispute/uncertainty where applicable. | A second report or model vote cannot invalidate a primary fact on its own. |
| Temporary fetch failure or isolated 404 | Retry/record accessibility uncertainty. | Do not infer source withdrawal or retract true facts solely from temporary inaccessibility. |
| Validated loss of all supporting evidence | Existing withdrawal path and correction ledger. | External correction sends retain channel authorization. |

Acceptance:

- An authoritative revision progresses from detection to a new version and visible correction history without per-item operator action when all existing publication gates pass.
- Replay, duplicate jobs and racing workers create at most one applied transition for the same action generation. Lease loss and simultaneous permission/situation changes cannot commit stale corrections.
- Unsafe content is suppressed without waiting for research completion. Suspension/holds remain effective, and missing replacement evidence never becomes a fabricated replacement.
- Undo is a new audited compensating transition through current eligibility; historical rows remain. It cannot restore revoked evidence or unsend an external message.

Guardian: `correction_pipeline`, `public_evidence_validity`, `explanation_grounding`. Compare affected inputs with current versions, pending deliveries and action outcomes. Requeue safe unfinished reassessment; suppress a known-invalid projection and escalate if the corrective action fails.

### AS-R05 — Verify public outcomes and external correction work

Implementation: extend branch visitor checks and persisted observations to inspect current HTML, history/corrections pages, any enabled feeds/exports, cached responses, queued messages and correction ledger entries. Separate factual correction completion from external follow-up completion. Draft correction notices from current eligible versions into existing review paths, with original destination, prior receipt and exact revision bound together.

Acceptance:

- A real browser verifies that the current public page and correction history show the replacement facts, scope, dates and explanation; superseded or withdrawn detail URLs do not present invalid content as current.
- Test anonymous, cached and direct-link access after rights revocation. Private operator evidence remains authenticated and retention-aware. Absence of a configured public origin/browser is unknown, not healthy.
- Each affected confirmed external send has idempotent correction work. Pending unsent copies are cancelled. External outcomes that remain unknown are reconciled or escalated, never blindly resent.
- Fulfillment still requires a confirmed same-channel/same-target receipt for the current eligible assessment. Provider acceptance, mailbox delivery and removal/editing of an old public post are different outcomes.
- A website repair may resolve while external correction work remains open. No action is marked fully recovered simply because a job returned success.

Guardian: `public_correction_consistency`, `delivery_correction_backlog`, `delivery_outcome_unknown`. Poll actual projections/receipts, dedupe incidents and escalate overdue reviewed sends or unsupported provider edit/delete operations. Do not auto-send a notice to clear an incident.

### AS-R06 — Make Guardian close the loop and survive application failure

Implementation: extend the existing alert path, retaining incident identities and audit history, rather than importing TV Insights' Guardian wholesale. Each finding names an allowed action, target generation, cooldown, attempt limit, expected postcondition and escalation owner. Add operator run/recheck/pause controls and a global research/application kill switch. Split monitoring cadence from repair execution; probes never run heavy research inline.

Existing `ops_heartbeat.py` is a starting point, not proof of supervision. Add independently run monitoring of progress, public reachability and alert delivery outside the application scheduler/worker. A heartbeat thread continuing to ping while the main loop hangs must not count as healthy; success must reflect recent useful progress or an intentionally idle healthy cycle.

Acceptance:

- Fault injection causes detect -> one bounded repair -> independent recheck -> resolution, with durable timestamps and evidence. Failed/exhausted repair escalates once per meaningful incident transition.
- Kill worker, hang scheduler, disable DB connectivity, break storage and disable the internal health job. The independent monitor reaches the configured recipient; the app cannot mark its own outage healthy.
- Recoveries generate a resolved event. Unchanged incidents do not repeatedly notify. Source-rights and known-invalid-content breaches trigger immediate suppression/escalation rather than waiting for a debouncing threshold.
- Pause stops new effects and cancels/defer-checks work already queued; it does not resurrect suppressed content. No automatic code edits, shell commands, dependency installs or service restarts are introduced.

Guardian: `research_progress`, `repair_effectiveness`, `guardian_probe_freshness`, `notification_delivery`, `resource_pressure`, plus independent scheduler/worker/public monitors. A stale or unavailable probe is unknown or failed according to its contract, never an implicit pass.

### AS-R07 — Qualify the pilot and release in stages

Implementation: extend the current `eval/` harness and focused PostgreSQL/PostGIS integration suites, then run a recorded staging pilot. Keep generated fixtures separate from editorially labelled held-out cases. Use real model outputs for model qualification; offline stand-ins establish regression behavior only.

Acceptance:

- Build at least 60 held-out research/correction scenarios, including at least 20 cases requiring abstention or escalation. Cover authoritative revisions, unchanged pages, period/scope mismatch, source disagreement/syndication, revoked rights, source removal ambiguity, malicious text, model outage, job crash, failed public projection and uncertain external send.
- Proposed pilot targets: at least 90% of answerable cases resolved correctly within the approved policy; at least 95% recall for actionable research/correction cases; 100% abstention/escalation on designated unsafe cases; zero critical unsupported facts, unauthorized collections/sends or cross-scope automatic corrections. Report numerator, denominator, exclusions, policy revision, source and model versions. These targets are proposals, not measured results.
- Preserve the existing launch-evaluation requirement documented in `eval/README.md`: at least 60 editorially labelled T1 and 40 T2 cases with real/recorded model answers. Passing the new research set does not replace that separate assessment gate.
- Run meaningful PostgreSQL concurrency tests for dedupe, leases, generation fencing, permission changes during capture/application, current-version races, crash replay and correction receipts. Validate migrations from the selected mainline and rollback-compatible application behavior on a disposable DB.
- Run actual desktop/mobile browser journeys, cache/revocation checks and independent-monitor drills against the exact staging SHA/schema. Qualify every enabled external provider separately.
- Roll out `off -> shadow decisions -> automatic approved-source research -> eligible website correction canary -> wider approved scope`. Shadow work may not alter public facts or external delivery. Use a minimum seven-day canary with recorded question/repair outcomes and all fault drills; elapsed time alone is not acceptance.
- Promotion requires assigned owners, completed applicable gates and measured resource headroom. If any critical violation occurs, disable application/research as appropriate, preserve evidence, suppress unsafe content and restore the previous application release without restoring invalid facts.

## Proposed operating limits and Guardian contracts

These are starting policy values for staging, not claims about current capacity or safe production budgets. Fit them within existing source limits, LLM reservations and shared-host measurements; stricter limits win. Do not purchase capacity or increase existing spend automatically.

| Contract | Initial proposal | Response |
| --- | --- | --- |
| Planner | Every 5 minutes; scan at most 100 candidates per pass with a durable cursor. | Track unswept age; resume next batch. |
| Research execution | One concurrent question; at most 10 new questions/hour and 30 primary fetches/hour across the research workload. | Count transport retries and redirects; enforce stricter per-source limits and global workload reservations. |
| Per-question allowance | At most 3 distinct candidate documents and 2 model decisions; no more than 3 transport attempts total per generation. | A supervisor rerun cannot reset the durable allowance. Record exhausted/unresolved. |
| Research lifetime | 24-hour deadline; positive evidence refresh follows source validity/release cadence. | Backoff unresolved no-result questions for 24 hours unless new evidence/permission generation arrives. |
| Guardian cadence | Lightweight checks every minute; public checks every 5 minutes. | Ordinary incidents open after two failures and resolve after two successes; immediate handling for confirmed unsafe output/rights violations. |
| Queue/progress | Eligible research or correction waiting over 15 minutes warns; over 60 minutes escalates. | Exclude explicitly paused/window/budget-waiting time from execution SLA, but report wall-clock staleness separately. |
| Known-invalid public output | Suppress at the next read through eligibility; verify via public probe within 5 minutes. | Critical incident if it remains publicly exposed. |
| Correction work | Approved internal action should settle within 15 minutes of validated evidence under healthy dependencies. External review backlog over 24 hours warns. | Do not bypass editorial review to meet a timer. |
| Repair cooldown | At most one active action per target generation; cooldown 30 minutes after a completed unsuccessful repair. | Existing narrow source retry retains its own policy; reconcile allowances to prevent stacked retries. |
| Independent monitor | Fresh useful-progress signal at least every minute, alert within 5 minutes of absence. | Validate actual notification receipt through dead and hung process drills. |
| Resource limits | Measure process/container CPU/RAM, DB latency, queue lag, bytes and model spend; set approved per-workload caps before canary. | Pause optional research under pressure; preserve public reads, critical suppression and other co-hosted applications. |

The new Guardian check IDs above are proposed names, not already implemented checks. Register each with its handler, cadence, disabled semantics, evidence payload, recovery rule and runbook. Reuse existing `sources_failing`, `jobs_dead`, `scheduler_stalled`, `queue_lag`, `series_reference_stale`, `storage_unavailable`, `email_outcome_unknown` and `social_outcome_unknown`; do not create duplicate incidents for the same underlying failure.

## Evidence required to call the capability complete

For each pilot question retain a rights-compliant trail of trigger -> question/generation -> attempts -> captured evidence/origin -> validated decision -> applied version/action -> public recheck -> external correction state, with timestamps and stable IDs. Retention deletion must redact expired text across those records while preserving permissible audit metadata.

Report question coverage and recall, correct resolutions, abstentions, incorrect automatic corrections, p50/p95 resolution time, repeated repairs, unresolved age, operator interventions, spend per resolved question and source fetch load. Separately report known public exposure after invalidation, external correction completion and independent alert receipt. Define eligible denominators before evaluation; do not improve success rates by silently dropping unresolved questions.

The final release record must identify local checks, CI, pushed/merged commits, deployed SHA/schema, live-provider results and editorial/operating acceptance separately. The work is complete only when the scoped automatic loop runs and recovers correctly on the deployed pilot, not when all modules or dashboards exist.

## Handoff

Next owner: engineering implements AS-R00 and AS-R01, then AS-R02/03; AS-R04 depends on validated evidence decisions. AS-R05 depends on the correction path and visitor-check baseline. Independent monitoring in AS-R06 should begin with AS-R00 and is required before active correction canary. AS-R07 gates every promotion.

Editorial owner defines the labelled cases, authority hierarchy and ambiguous-case policy. Operating owner supplies staging, approved source configuration, monitor/notification destinations, resource limits and incident response. Existing authorization permits preparing this plan; it does not establish those deployment inputs or new source/vendor permissions.

Plan validation performed: current-source inspection, remote-ref refresh and comparison, existing policy/queue/correction/runner/Guardian/evaluation contract review. No application tests were run because this deliverable changes documentation only. Remaining unknowns are live configuration, provider and source qualification, staging/production state, measured load and owner acceptance. No implementation estimate is presented as measured effort.
