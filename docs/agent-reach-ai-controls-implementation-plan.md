# Agent Reach, AI routing, resource scheduling and dashboard implementation plan

Prepared: 2026-10-02. Initially written as a handoff; implementation status is recorded in Section 11.

## 1. Objective and baseline

Implement optional Agent Reach discovery, configurable AI providers and external agent services, operating windows for expensive work, and complete operator visibility over supported operational functions. Agent Reach must have a persistent dashboard on/off switch. Preserve source permissions, evidence provenance, spending limits, job fencing and publication controls.

The source findings and verified answers below are the initial audit snapshot. Section 11 supersedes them for current source state. No running dashboard, live provider API, runner installation or deployment has been exercised.

Audited checkout: AfricaSignal-merge, branch codex/production-readiness-followup, commit fc056fe417176f44016071fb541538eefdd79725. The remote branch matched that commit. Remote main was edb3e81f773e8cb84609cd56dd3bbf426e616354. These baselines differ: AI reservation and job fencing improvements are on the follow-up branch used for this implementation. Revalidate the branch against current main before publishing.

### Verified answers at initial audit (historical snapshot)

| Question | Audit-time state | Source anchors |
|---|---|---|
| Is there an AI abstraction? | Yes: LlmProvider protocol and LlmAdapter.complete_json, schema validation, cache, accounting and budgets. | src/africasignal/llm/adapter.py, budget.py, schema.py, cache.py |
| Is there a full AI router? | No. make_provider constructs Anthropic only. Purpose/model selection is cached YAML, without dashboard routing, multi-provider fallback or circuit breaker. | llm/adapter.py, llm/config.py, config/llm.yaml |
| Can the dashboard configure AI? | Partly: Anthropic key, daily USD budget and per-job token limit. No provider/model/effort/price editor, AI global switch or external-agent registry. | settings_store.py, web/routes/admin.py, templates/admin/settings.html |
| Are provider APIs live-qualified? | Not established. Anthropic provider source explicitly describes stub-client verification and absent live qualification. Model IDs/prices require verification. | llm/anthropic_provider.py, config/llm.yaml |
| Is Agent Reach integrated? | No integration or control found in the inspected source/handler registries, settings or admin surfaces. | sources/, jobs/handlers/, settings_store.py, admin routes |
| Can heavy work run only during selected hours? | No operator policy yet. Delayed run_at and source intervals exist; periodic schedules are hardcoded. Workers claim any due job. | jobs/scheduler.py, queue.py, worker.py |
| Does the dashboard configure every function? | No. Many daily workflows exist; scheduling, routing, source pacing, OCR and other operational settings are missing. | Inventory below |

Agent Reach provisions/selects access tools; underlying tools perform acquisition directly. It is not AfricaSignal's model gateway or evidence pipeline. Current upstream metadata declares beta status. Pin a reviewed commit and exact backend dependencies; do not install from moving main at runtime. Primary references: [upstream design](https://github.com/Panniantong/Agent-Reach/blob/main/docs/README_en.md), [package metadata](https://github.com/Panniantong/Agent-Reach/blob/main/pyproject.toml).

## 2. Architecture decisions

1. Separate model providers, bounded external agent services, and Agent Reach access capabilities. An external agent is not simply another model API.
2. Isolate Agent Reach in a separate service/container. Give it no application DB credential, publication credential, reader data, host filesystem or Docker socket. Do not accept arbitrary shell commands from operators or models.
3. Use existing FastAPI/Jinja admin architecture and durable PostgreSQL queue. Avoid a frontend rewrite or second scheduler.
4. Store typed/versioned runtime policy in the database. Keep deployment deny locks and bootstrap dependencies outside dashboard control.
5. Prefer deterministic parsers, deduplication and cached results before paid AI.
6. Discovery results are candidates. Agents cannot approve rights, change source ownership, publish, send messages, change configuration or write authoritative measurements.
7. Keep health, expiry, withdrawal/invalidation, essential deletion/retention, authentication and recovery quarantine continuous/deadline-driven.

Target flow:

    Dashboard + authenticated administration API
                        |
           validated/versioned control service
               |                     |
      workload admission       provider/agent registry
               |                     |
       durable job queue ---- AI router / agent gateway
               |
       isolated Agent Reach runner
               |
       candidate review -> approved source connector
               |
       evidence capture -> assessment -> publication policy

## 3. Dashboard coverage inventory

"All functionalities" means every supported operational function has a control or a clear read-only explanation of its deployment/code owner. It does not mean unrestricted editing of security invariants, executable commands or database internals.

| Function | Current console coverage | Required disposition |
|---|---|---|
| Source pause/resume, owner, permission versions | Present | Preserve; show approval/review expiry and schedule together; recheck permission at acquisition. |
| Source interval, hourly limits, feed URL, languages | No general editor found; seeded/stored values | Add bounded editor, validation, audit, effective value and next-run preview. Adapter changes need a controlled review/migration. |
| GDELT/domain acceptance | Domain reject/add exists; cadence hardcoded | Safe cadence/lookback/batch controls; accepted new domains remain inactive and unapproved. |
| Agent Reach | Missing | Global switch, backend switches, readiness, windows, quotas, candidates and diagnostics. |
| AI providers/credentials | Anthropic key only | Profiles, capability tests, encrypted credentials or secret references, rotate/revoke and health. |
| Models/effort/prices/routes | YAML only | Versioned catalogue, reviewed prices, purpose routing, approved fallbacks and activation. |
| External AI agent services | Missing | Endpoint/transport/auth, scoped capabilities, bounded task contract, test, disable/cancel and audit. |
| AI spending | Daily/per-job limits and cost view | Preserve; add total/provider/purpose/agent caps, concurrency, actual/reserved/uncertain usage and reconciliation. |
| Windows/resource limits | Missing | Days/timezone/windows, preview, runtime admission, backlog and distributed limits. |
| Jobs | Counts/dead jobs/retry | Filters, workload/deferral reason, safe cancel, bounded run-once/drain and retry controls; no raw payload editor. |
| OCR/document processing | Code limits; OCR inside adapter processing | Capability enablement, bounded pages/bytes/runtime and heavy-work windows; first resolve capture/OCR coupling. |
| Publication/assessment | Suspend/resume, withhold/release/withdraw, range review | Preserve; distinguish from AI/Reach switches; show policy version/reasons. No disable-evidence toggle. |
| Policies/item catalogue | Operator policy add/retire; base YAML read-only | Reviewed versioned overrides for supported labels/keywords/materiality; preview consequences; validate units/mappings. |
| Places/coverage/aliases | Data/config driven; no general editor found | Reviewed coverage/alias workflow; structural changes remain controlled imports/migrations. |
| Email/digests/channel drafts | Email settings/manual post marking | Delivery enablement, digest day/time, test and health; preserve suppression/unsubscribe; X/WhatsApp remain manual drafts unless separately authorized. |
| Evidence/backup storage | Bucket/credential/retention settings | Show readiness and last verification. Bucket/endpoint replacement requires staged migration, not instant pointer change. |
| Backup execution/restore | Host scripts/external schedule; some console settings | Show schedule owner/status; optional constrained backup controller. Restore/key recovery stays an infrastructure procedure, not a web shell. |
| Retention/deletion | Source permissions and feedback retention | Deadline/backlog/last enforcement visibility; policies for new candidates/tasks cannot override source rights. |
| Alerts/independent heartbeats | Alerts exist; targets environment-only | Monitor configuration/status and safe deployment-controller test; monitor remains independently owned. |
| Identity/legal metadata | Present | Preserve. A legal-review checkbox is an operator assertion, not approval evidence. |
| Rate/cache/proxy controls | Some proxy settings; other limits in code/deployment | Bounded operational controls and deployment status; retain security floors and constrained trust configuration. |
| Operators/roles | Host CLI; console uses admin/editor | Optional account administration with step-up auth, immediate revocation and last-admin protection. Bootstrap remains host-managed. |
| Metrics/audit | Views exist | Resource/provider/decision telemetry, configuration revisions, sanitized export. |
| DB/root key/environment/TLS/deployment | Intentionally not editable | Sanitized health/version/owner; no live edit of DB URL, encryption root key, environment, trusted origins or deployment image. |

Deliver a machine-readable manifest covering every setting registry field, handler kind, editable business setting and deployment-only setting. Expand the inventory after auditing remaining constants. Each entry specifies owner, data type, bounds, default, sensitivity, runtime consumer, UI/API path, apply timing and test. No unexplained gaps at completion.

## 4. Agent Reach on/off and discovery

### Required switch semantics

- Default agent_reach.enabled=false, independent of ai.enabled and publication suspension. A deployment deny lock always wins and is visible.
- Display desired and effective state: Off, Waiting for window, Ready, Running, Draining, Budget blocked, Unhealthy or Deployment blocked.
- Disable commits a new control generation. No new task may be admitted after that commit; every worker rechecks immediately before dispatch.
- Late results from an old generation cannot become accepted evidence. Local tasks receive cooperative cancellation followed by bounded process termination. Remote cancellation failures remain visible.
- Pending jobs remain durable/deferred without retry consumption. Offer a separate audited cancel-pending action. Re-enable drains a capped, jittered batch.
- Cancellation never discards already incurred or uncertain costs. Accounting accepts late usage settlement even when result acceptance is blocked.
- Initial pilot uses only fixed, reviewed search and approved official-video capabilities where rights permit. No blanket social/browser-cookie enablement.

### Runner contract and isolation

Build an application-owned typed service; upstream does not provide AfricaSignal's ingestion HTTP API.

Request fields: task_id, idempotency_key, control_generation, config_revision, approved capability, query/template or URL, source/domain policy, deadline, maximum results/output bytes and secret reference. No executable path, raw shell command or arbitrary environment map.

Response fields: task/generation identifiers, backend and version, requested/final/original publisher URLs, platform identity, retrieved_at, claimed publication date with provenance, bounded metadata, permitted attachment/content hashes, partial status, sanitized error and metered usage.

Authenticate internal transport with mutual authentication or short-lived scoped tokens. Execute only a fixed backend registry using argument arrays, strict validation, temporary-file isolation, hard deadlines and output limits. OS/network egress controls must cover subprocesses, redirects, DNS rebinding and indirect hosted-reader URLs; application URL validation alone is insufficient.

Discovery metadata must be permitted by the backend's approved terms/data policy. Fetching candidate pages, transcripts or full content requires source acquisition permission beforehand. Reviewing after collection does not retroactively authorize collection. Unapproved domains remain bounded URL/metadata leads. Store original publisher provenance separately from transformed access-service output.

Persist candidate records with dedupe, domain/source identity, query revision, timestamps, retention and reviewed decision. Acceptance maps to an approved source or creates an inactive source awaiting permission. Only qualified connectors pass permitted content through current capture/process functions. Never label transformed text as original publisher bytes or bypass quotation/retention controls.

## 5. AI router and external-agent wiring

### Provider/model routing

Create versioned provider profiles: adapter type, approved endpoint, enabled state, credential reference, timeouts, request/token limits and circuit health. Model catalogue entries use provider-qualified identities, model/version, JSON-schema/effort capabilities, context/output limits, data policy and reviewed pricing/effective date.

Purpose routes initially cover claim_extract and explain; research purposes require an explicit contract. Each route has an ordered approved provider/model list, constraints and fallback rules. Choose deterministically among qualified routes. Models cannot select their own unbounded escalation.

Preserve current Anthropic behavior through a migration/bootstrap profile. Newly added provider profiles default disabled until qualified. OpenAI-compatible endpoints need a distinct adapter and declared compatibility; the label alone does not establish matching API semantics. Verify model identifiers, prices and request fields using current official provider documentation/API during implementation.

Integrate routing below complete_json so accounting, cache and schema validation remain mandatory. Cache identity includes provider/endpoint deployment, model/version, effective effort, prompt/schema identity and all response-semantic parameters. Identical model strings on different endpoints must not collide. Record route/config revision and each attempted call.

Fallback only on approved transient failures within remaining deadline and quota. Refusal, schema failure or failed factual checks cannot silently route to a more permissive provider. Ambiguous network outcomes remain uncertain until reconciled.

Reserve aggregate upper-bound spend before calls; SDK retries must be constrained or accounted for. Enforce durable total/provider/purpose/task caps atomically across workers. A provider budget must not bypass the total ceiling. If usage types cannot be safely bounded, block unattended calls rather than promise a hard cap.

Expose explicit capability/test calls with estimated maximum cost and actual accounting. Listing a model or passing a synthetic JSON test does not qualify editorial accuracy.

### External agents are a separate contract

API keys and model selectors do not constitute an autonomous-agent integration. Define a transport registry. Initial protocol: bounded authenticated HTTPS submit/status/result/cancel. MCP or provider-specific transports require separate qualified adapters; no universal agent API is assumed.

Agent profile: approved endpoint/transport, credential reference, scopes, allowed purpose/tools/domains, data classification, maximum steps/duration/output, enforceable cost ceiling, concurrency and schedule. Source content cannot override policy.

Durable states: pending, admitted, running, cancellation_requested, succeeded, failed, expired, cancelled and outcome_unknown. Record external task ID, idempotency key, generation, config revision, lease/fencing identity, deadline and reservation.

Callbacks are signed/authenticated, replay-protected and bound to task/profile/generation. Poll when callbacks cannot meet the contract. Duplicate/out-of-order completion is idempotent. Profile disable blocks admission and revokes scoped credentials where supported.

Do not use self-reported cost as a hard bound. Require enforceable provider quotas/fixed ceilings and reconciliation; otherwise permit only a capped supervised pilot. Agents cannot publish, administer the application, approve rights or access reader data.

### Administration API proposal

Private namespace separate from public /v1 reader APIs:

| Resource | Supported operations |
|---|---|
| /admin/api/ai/providers and /models | List/create/version/test/activate/disable; credentials write-only |
| /admin/api/ai/routes | Preview/save/activate with expected revision |
| /admin/api/agents and /agents/{id}/tasks | Profiles/test/status/bounded submit/cancel/disable |
| /admin/api/agent-reach/control and /backends | Desired/effective state, on/off, capability configuration/readiness |
| /admin/api/discovery/candidates | List/review/reject/map to source |
| /admin/api/workloads and /schedules | Limits/windows/preview/run-once/deferred backlog |
| /admin/api/configuration/coverage | Sanitized manifest/ownership/applied revisions |

Dashboard session requests retain CSRF/origin enforcement. Machine administration, if required, uses distinct scoped expiring/revocable service credentials; do not reuse browser cookies or introduce one unrestricted admin key. Agent callbacks use separate auth. All mutations use shared services, atomic validation, permissions, optimistic revision checks and redacted audit. Reads never return secrets or unrestricted evidence/prompts.

## 6. Operating windows and resource conservation

Expensive work can be admitted only during selected periods. Scheduling shifts load; it does not inherently reduce token consumption or a fixed VM bill. Reduce total work through deterministic processing, dedupe, cache, bounded batches, stale-task cancellation and measured routing. Infrastructure savings require a deployment controller to scale heavy workers separately from continuous services.

### Policy schema

Per workload: enabled, IANA timezone (default Africa/Lagos), weekdays, one or more start/end windows, cadence where relevant, priority, maximum concurrency, per-run items/pages/tokens/requests, soft/hard runtime, maximum backlog age, missed-run policy, maximum catch-up batch and dated override.

Store instants/accounting in UTC. Lagos scheduling must not silently change the current UTC budget-day boundary. Validate overnight/overlapping/empty windows and end-exclusive boundaries.

For zones with DST: ambiguous start uses earlier instant, ambiguous end later instant; nonexistent times advance to the first valid instant. Dedupe prevents double dispatch. Preview actual upcoming UTC and local execution times.

Suggested pilot defaults, editable and not a launch freshness commitment:

| Workload | Initial disposition |
|---|---|
| Agent Reach research | Off; when qualified, daily 01:00-04:00 Lagos, concurrency 1, capped batch |
| LLM extraction/explanation | Preserve current eligibility at migration; allow windows after freshness preview. Separate urgent approved work from optional backfills. |
| OCR/bulk imports | Heavy queue with explicit bounds; new unsupported paths off |
| Approved source discovery | Preserve present cadence unless operator changes it; do not silently make all news collection nightly |
| Backup | Independent nightly infrastructure schedule/heartbeat; preserve required RPO |
| Withdrawal/invalidation, expiry, deletion/retention, health, authentication/recovery | Continuous or deadline-driven outside research-window restrictions |
| Outbox | Login/correction/suppression promptly serviced; digest timing separately configurable |

### Admission and runtime rules

1. Catalogue every handler as continuous or schedulable with resource limits, priority, freshness requirement and policy. Unknown kinds fail closed and alert.
2. Scheduler plans idempotently using slots/revisions. Missed-run policies explicitly skip, coalesce or replay a bounded batch. Jobs survive restart.
3. Queue admission excludes disabled/out-of-window work, checks worker capability and distributed concurrency, and reserves capacity for continuous work. Policy deferral is not failure: no attempt consumption, hot loop or leased sleeping worker.
4. Recheck before expensive I/O/model calls, including retry, source approval/resume and backfill. Run-once is bounded/authorized/audited and cannot override kill switches, rights, quota or quarantine.
5. Keep network politeness separate: source/robots limits remain ceilings. Add shared domain pacing before multiple network workers; existing pacing is process-local.
6. No new expensive step starts at/after window end. Pilot tasks have deadlines at window close with checkpoint/cancel/termination. An optional finish-current policy must state its maximum overrun. Remote cancellation cannot be guaranteed; show outstanding work and continue accounting.
7. Concurrency/permits are durable across replicas, not local semaphores. CPU/RAM/process/egress bounds require runner/container enforcement. Dashboard shows desired versus deployment-applied limits/acknowledgement; editing a number does not resize a container.
8. Show oldest deferred age, next eligible time, drain estimate and freshness impact. Alert independently on excessive backlog. Expiry continues; deferred extraction never receives a falsely refreshed check timestamp.

### Capture/OCR coupling must be resolved

process_document captures and invokes a source adapter in the same handler; NERC processing may perform OCR. Separate fetch/OCR/extraction windows require an explicit split/checkpoint refactor.

Permitted retained documents can resume from evidence storage. Non-retained source bytes currently exist only transiently: do not create a persistent staging copy for scheduling convenience. Finish permitted processing within an admitted bounded task, or refetch later under permissions/pacing. If this cannot fit, keep the capability/source blocked with a clear reason. Enforce temporary-file expiry and crash cleanup.

## 7. Configuration and dashboard contract

- Extend small scalar settings through existing settings_store. Use normalized/versioned entities for profiles/routes/schedules/candidates/tasks, not an opaque arbitrary JSON configuration blob.
- Record revision/actor/time/validation/applied revision. Validate references before transactional activation; reject stale concurrent edits. Retain audited rollback revisions.
- Seed existing model/source defaults deterministically. Subsequent seed/import cannot overwrite approved runtime edits. Workers refresh configuration instead of permanent lru_cache state.
- Existing generic secret validation allows 8-500 non-whitespace characters and is unsuitable for cookie exports/structured credentials. Use profile-specific encrypted schemas or secret-manager references; bounded input, redaction, rotation and recovery. Show only presence/owner/expiry.
- Admins configure/enable/resume. Editors inspect authorized status and use safe stop controls only. Server endpoints enforce roles; hidden links are insufficient. Step-up auth for credential/endpoint/privilege/bulk changes.
- Preserve the current console style. Group navigation into Overview, Sources/Discovery, AI/Agents, Jobs/Schedules, Publication, Storage/Recovery, Monitoring and Administration.
- Show each control's scope, effective value/source, blocked reason, revision and apply timing. Include next-run previews and read-only deployment ownership.
- Accessible switch/save flow: labelled control, keyboard support, text with colour, server-confirmed saved state, preserved focus, contextual status announcement, field-linked errors and summary. Pending deployment changes remain Pending until acknowledgement.
- Endpoint/storage/credential changes need concrete preview/test/activation. Clearing a console key must show whether environment fallback remains effective; clearing is not disabling.
- Bound operational controls. Rights, evidence requirements, quarantine, minimum security limits and trust boundaries cannot be disabled through arbitrary UI values. More permissive editorial rules require reviewed versions and regression evaluation.

## 8. Execution order and file map

Implement reviewable PRs in sequence. This plan does not itself authorize delegation or live deployment.

| Package | Work/file areas | Exit evidence |
|---|---|---|
| P0: baseline/coverage | Revalidate main/follow-up; preserve remediation; generate field/workload manifest | Exact commits, complete ownership/classification |
| P1: control foundations | Typed schemas/services/revisions, Alembic, audit/RBAC, effective state, AI/Reach locks; settings_store/models/admin | Atomic migration/validation, stale-edit rejection, default-off switch, redaction |
| P2: workload admission | New jobs/policy.py and schedules.py; queue/worker/scheduler, distributed leases, source editor | Queued/manual paths respect windows; continuous capacity and restart/concurrency tests |
| P3: AI router | llm/router.py, adapters/profiles, cache/accounting extensions, cost view | Anthropic compatibility preserved; configured adapters qualified; no cap/cache bypass |
| P4: external-agent gateway | Versioned profile registry, encrypted credentials, exact HTTPS task protocol, idempotency lookup/cancel, bounded public-data tasks, reservations and supervised dashboard | Migration, role/secret-redaction/admission/lifecycle tests; service declarations and remote enforcement still require independent staging qualification |
| P5: Reach runner | Separate pinned image/client, fixed backends, egress policy, candidate workflow/handlers | Default-off, disable races, rights before retrieval, no production secret exposure |
| P6: dashboard completion | Admin routes/shared API services/Jinja templates, catalogue and monitoring controls | Manifest coverage, real consumers, browser/keyboard/mobile and role tests |
| P7: qualification/handoff | Runbooks/rollback/staging-plan update/metrics/editorial samples | Exact-release evidence and truthful live/human acceptance ledger |

Existing tests to extend: test_settings_console.py, test_admin_console.py, test_admin_ops_security.py, test_jobs.py, test_llm_adapter.py, llm/test_config_and_budget.py, llm/test_providers.py, test_admin_csrf.py, test_settings_validation.py and source/capture/retention tests. Add behavior-focused coverage, not implementation-mirroring assertions.

## 9. Mandatory acceptance cases

### Switch/policy

- New/migrated installation keeps Reach off. Scheduler/direct enqueue/retry/source resume/callback cannot bypass disabled state.
- Off during queued/dispatching/running work on two workers blocks new admission and late-result promotion. Cancellation bounded; unknown remote outcomes visible/accounted.
- Restart/re-enable drains once with bounded batch; deferral preserves retries. Configuration/database failure never defaults to enabling expensive work.
- Reach, AI and publication controls have distinct scope. Reach off does not disable direct approved sources; AI off stops new paid calls; publication suspension still blocks publication/notifications.

### Scheduling/resources

- Overnight/weekend/exact endpoints/multiple windows/invalid overlap/no window/timezone/DST/missed tick/crash cases.
- Concurrent schedulers/workers cannot double-dispatch or exceed task/domain/provider limits; policy revision invalidates stale permits.
- Lease loss fences late results; old cleanup cannot remove a new permit.
- Fallback/SDK retry/run-once/handlers respect windows. Cached results may be reused without calls when their workload allows; explain this behavior in effective state.
- Heavy backlog cannot starve withdrawal, auth delivery, expiry, deletion/retention or monitoring.
- Real subprocess/child cleanup, deadlines, memory/output bounds and retained/non-retained source handling; mocks alone cannot prove OS enforcement.

### Provider/agent security and accounting

- Missing/revoked keys, unsupported model/effort/schema, SSRF/redirect violations, 429/5xx, refusal/malformed output, ambiguous timeout and multiple billed attempts.
- Global/purpose/profile/task reservations remain atomic; uncertain spend is not auto-freed. Cache isolates endpoints/parameters. Actual usage/billing dimensions match qualified provider semantics.
- Forged/replayed/wrong-task/wrong-generation callback; duplicate result; key rotation and remote cancellation failure.
- Prompt injection, shell strings, private URLs and forged policy in source content cannot alter tools/network/routing.
- Agents have no publication/configuration authority.

### Dashboard/API

- Every editable manifest entry has validated UI/API mutation, audit, effective state and a runtime consumer. Every noneditable item has a reason/owner.
- Admin/editor/service scopes, session revocation, CSRF/origins, stale-edit conflicts and redaction across page/API/audit/log.
- Browser evidence on mobile/desktop: keyboard switch/save, focus, validation, next-run preview, pending/applied state, contextual announcements and critical controls reachable.
- Bucket migration cannot orphan evidence; research disable cannot defer deletion deadlines. Credential presence alone cannot make an unsupported integration Ready.

## 10. Rollout and definition of done

Keep prior authorized scope: prepare repeatable staging instructions only. A host, bucket, account or public deployment is not implied.

1. Integrate prerequisites with reviewed PRs; run required checks on exact implementation commit using disposable test DB/object store. Retain documented readiness blockers.
2. Local rehearsal with fake/inert profiles and Reach off: switch, windows, crash, lease-loss, callbacks and cancellation.
3. Staging runbook: publication suspended, approved sources, test recipients, owner-provided authorized secrets. No secrets in chat/doc.
4. When separately authorized/configured, qualify one model provider, one agent transport and one or two Reach capabilities on exact image digest. Record real resource/network enforcement and human rights/editorial/privacy acceptance.
5. Agree sample/time and thresholds before pilot. Measure additional accepted relevant Nigerian energy/food leads, freshness, numeric/citation accuracy, duplicates, reliability/recovery, queue age, actual/reserved spend, CPU/RAM and cost per accepted lead. Do not invent savings.
6. Rollback: disable new profiles/Reach/routes, cancel pending work, restore previous activated configuration; quarantine late results, retain accounting/audit. Use additive forward-compatible migrations and reviewed restore procedures.

Done: implementation/migrations complete; coverage manifest has no unexplained function gap; controls affect actual execution paths; supported live integrations have dated evidence; required operational/human acceptance is recorded. If live/human qualification is unavailable, deliver complete source plus pending evidence ledger, not a production-readiness claim.

### Paste-ready instruction for the implementation agent

> Implement this plan against revalidated AfricaSignal source, preserving production-readiness hardening. Start with the coverage manifest/control foundations, then workload admission, AI routing, bounded external-agent gateway, isolated Agent Reach and dashboard completion. Reach defaults off; disable blocks queued/manual/retry dispatch and fences late results. Keep rights, spend reservations, quarantine and publication gates mandatory. Keep health/withdrawal/auth/retention continuous. Deliver reviewable PRs, migration/concurrency/browser evidence, rollback instructions and truthful pending acceptance. Prepare staging instructions only until deployment is separately authorized/configured. No arbitrary runtime tool installs, exposed secrets, unsupported transports marked ready or live/editorial claims from mocks.


## 11. Implementation status as of 2026-10-02

Implementation is present in the local follow-up checkout. It has not been committed, pushed, merged or deployed. The following status is about source only; it does not close runtime, security, rights, legal, editorial or accessibility acceptance.

| Package | Current source state | Remaining acceptance |
|---|---|---|
| P0: baseline and coverage | Revalidated follow-up checkout; added a read-only configuration/job ownership manifest and dashboard. Static inventory mapped all 34 registered settings and all statically declared handlers, including the deletion handler registered by constant. | Rechecked current GitHub main before updating the existing PR; main is an ancestor of this branch and there is no merge divergence. The configuration-coverage endpoint and page response remain browser-unverified. |
| P1: control foundations | Added versioned workload controls, migrations 0036–0038, audited settings, task/candidate records, source pacing overrides and revision-conflict handling. | Applied upgrade through 0038 on disposable PostGIS; the Linux integration fixtures also exercised repeated migration downgrade/upgrade. A deployment rollback rehearsal against real backup data remains staging acceptance. |
| P2: workload admission | Added timezone-aware weekly windows, persisted enable switches, queue admission serialization, retry-safe deferrals, AI/processing gates, Agent Reach task concurrency and a configurable rolling 24-hour request cap. GDELT cadence/replay bounds and weekly digest time are configurable. | The full Linux suite passed admission, schedule policy, PostgreSQL concurrent claims, fencing and retry cases. Cross-container contention and live restart behavior remain unqualified. Worker CPU/RAM/process ceilings and shared-domain pacing across replicas remain deployment-owned. |
| P3: AI router | Added purpose-based Anthropic/OpenAI routing, dashboard key/route/budget controls, route-aware caching, provider adapters, and runtime AI window checks. Existing reservation/accounting paths remain in use. There is no automatic provider fallback; each route is explicit and has a reviewed price entry. | Fake-provider PostgreSQL contract tests and the full Linux suite passed. Qualify each selected model against the deployed key, schema, token usage and current price. |
| P4: external-agent gateway | Added an off-by-default profile registry, encrypted bearer credentials, HTTPS health checks, revisioned capability/domain/step/output/task/concurrency/cost limits, a strict versioned task API, idempotent submission and reconciliation, cancellation, retention and admin controls. The contract is documented in docs/external-agent-protocol.md. | The protocol is not a universal connector for arbitrary vendor APIs. A service must implement this contract or receive a reviewed adapter. Health limit declarations are self-reported; independently test enforcement, billing, data access, privacy and cancellation in staging. |
| P5: Agent Reach | Added the dashboard switch, runner URL/token controls, authenticated health, bounded task submission/cancellation/recovery, candidate review through permissioned RSS sources, and default-off scheduling. This checkout now contains a separate HTTPS bridge, non-root container, SQLite task store, fixed official YouTube Data API metadata adapter, allowlisted egress proxy, and repeatable Compose configuration. The bridge does not install the upstream Agent-Reach CLI or provide general social/browser access. | The Linux runner image built locally as africasignal-agent-reach:local (digest sha256:241e7796230c4dbee638f6dea4a8adeb0eeedb9ee9e12ecff9640c4b7548de49). Stage behind an authorized HTTPS route and qualify network-level egress, OS resource limits, API-key restrictions, rights, editorial quality, privacy, quotas, and cost. No live runner or source-rights acceptance exists here; the request cap is not a monetary budget. |
| P6: dashboard coverage | Added AI/automation and Agent Reach controls, source pacing controls, provider route settings, an external-agent profile/task console, plus a read-only coverage view separating console-, code-, deployment- and human-owned settings. External-agent switches remain distinct from AI and Agent Reach. | Linux app startup and OpenAPI route registration passed. Exercise admin roles, CSRF, keyboard/mobile accessibility, validation, save conflicts and screen-reader announcements in a browser. |
| P7: qualification and handoff | Updated this plan and wrote the bridge contract. The previously authorized scope is a repeatable staging plan only. | No staging host, production backup bucket, provider credentials or live runner were configured here. The local encrypted restore drill succeeded against a disposable PostGIS database, comparing 39 tables at schema 0038; it did not test bucket objects, deletion replay or on-call. This task is authorized to prepare a repeatable staging plan only, so no deployment was attempted. Finish exact-release-digest, object/deletion recovery, kill-switch/on-call drills and human acceptance before launch. |

The supported AI model providers are Anthropic and OpenAI. The dashboard can choose reviewed provider/model routes for supported purposes. The Agent Reach dashboard switch controls the isolated bridge described above, which currently implements only public YouTube metadata search. It does not install the upstream Agent-Reach CLI, enable browser/social backends, or create a generic API for arbitrary agents. Each added capability needs a reviewed adapter with its own authentication, rights, policy, metering and lifecycle.

The Agent Reach defaults are off, one concurrent active task, and ten task starts per rolling 24 hours in AfricaSignal. The runner independently defaults to ten YouTube search calls per Pacific quota day and will not exceed the validated provider ceiling of 100. Operators may raise concurrency only up to the runner-reported limit of four and raise the local request quota within the stated bounds. There is no trustworthy monetary cost meter or spend ceiling. Keep the capability disabled until source rights, cost and privacy acceptance are recorded.

Verification for the implementation source: full Linux pytest against disposable PostGIS — 2,111 passed, 5 skipped in 18m43s; RUN_NETWORK_TESTS was unset, so live boundary-file downloads were not attempted. Ruff check and format check passed (364 files); mypy passed for 176 source files; compileall and git diff --check passed. Alembic upgrade reached 0039 (head) on disposable PostGIS. Compose configuration validation passed. The CI-style application image build remains unverified in this environment: the default package index did not resolve pinned lxml 6.1.3, although [PyPI publishes a CPython 3.12 Linux x86-64 wheel](https://pypi.org/project/lxml/6.1.3/); a retry using PyPI passed dependency resolution but failed when container DNS could not resolve deb.debian.org for OS packages. No pins or Dockerfile were changed. Staging deployment, browser/accessibility acceptance, network-level egress enforcement, live provider calls, real backup-bucket/object recovery, deletion replay and on-call drills remain unverified. No production or staging claim follows from these source changes.