# AfricaSignal growth implementation plan

Date: 2 October 2026. Status: implementation handoff; no application implementation or deployment performed by this document.
Companion: [execution packets](africasignal-growth-execution-packets.md).

## 1. Objective and scope

Adapt the three TV Insights additions: optional Agent Reach acquisition, advertising/sponsorship, and contextual AI commercial intelligence. Reuse architectural patterns and execution discipline, not TV-specific schemas, SQLModel services, EPG/carriage tasks or assumed implementation status.

Preserve AfricaSignal's energy/food scope, Nigerian geography, editorial independence, source permissions, evidence provenance, publication suspension, withdrawal propagation, queue fencing and spending bounds. Commercial activity must never change evidence state, severity, source credibility, factual conclusions or organic search order.

This document authorizes no provider activation, outreach, prices, live migration or deployment. The user's current staging scope remains a repeatable deployment plan only. Prepare and verify local source without inventing credentials; record unavailable external acceptance explicitly. Implementation authorization is separate from this planning request.

## 2. Source baseline

Repository: ainatunde/AfricaSignal.
Inspection checkout: C:/Users/aina_/.gemini/antigravity/scratch/AfricaSignal-merge.
Fetched remote main: dfa16550904b36a6990be6d4cd644b4fa7a7af4c.
Local branch: codex/production-readiness-followup, HEAD b14415a933bc3b4befe9f08d4f0929e670944098.
Local committed tree has no content diff against the fetched main; ancestry differs because main contains the merge commit. Refresh main before execution, preserve handoff files/unrelated work, and use an isolated implementation branch.

Historical evidence: preceding Linux verification reported 2,111 passed, 5 skipped; merged-main CI subsequently succeeded. These are prior results, not checks rerun for this plan. Live/runtime, editorial, rights, privacy, browser and operational gates remain separate. Refresh PRODUCTION-READINESS-AUDIT.md and docs/agent-reach-ai-controls-implementation-plan.md rather than assuming historic findings are current.

| Concern | Existing source | Required treatment |
|---|---|---|
| Model router | src/africasignal/llm/adapter.py, llm/config.py, config/llm.yaml | Extend reviewed purposes; supported providers are Anthropic/OpenAI. |
| AI accounting | llm/budget.py and adapter independent accounting sessions | Reuse atomic PostgreSQL reservations, uncertain outcomes and per-job limits. |
| Scheduling | jobs/policy.py, jobs/queue.py, operations/workloads.py | Extend registered job/control mappings; retain claims, leases and fencing. |
| Agent Reach | operations/agent_reach.py, jobs/handlers/agent_reach.py, runner/agent_reach_runner/ | Existing isolated bridge has fixed official YouTube metadata backend, not general upstream CLI access. |
| External services | external_agents/, docs/external-agent-protocol.md, /admin/agents | Bounded protocol; arbitrary external agents are not automatically compatible. |
| Editorial | models/evidence.py, models/claims.py, models/assessments.py, sources/permissions.py, assess/ | Keep commercial projections independent of authoritative editorial data. |
| Operator controls | web/routes/admin_automation.py, admin_agent_reach.py, admin_coverage.py, settings_store.py | Reuse authorization, CSRF, audit, expected revision and effective-state patterns. |
| Sponsorship | No equivalent sponsor/campaign module found in inspected source | Build one AfricaSignal-owned commercial subsystem. |
| Privacy/security | web/templates/privacy.html, web/security_headers.py | No-advertising wording and self-only CSP need deliberate, accurate changes before activation. |

## 3. Milestones and order

M0: refreshed baseline, editorial invariants, surface policy and typed contracts.
M1: acquisition extension for one concrete useful backend; existing bridge retained.
M2: first-party direct sponsorship, default off, manual flat-fee process and measured reporting.
M3: deterministic contextual matching and operator-reviewed package drafts.
M4: optional scheduled AI classification/narratives through existing accounting.
M5: optional programmatic adapter with consent/configuration boundaries.
M6: qualification dossier and repeatable staging instructions; external acceptance recorded separately.

M2/M3 do not depend on an additional collector or live advertising account. Keep acquisition and commercial chains independently executable. A single coding model can execute their reviewed packets sequentially.

## 4. Acquisition contract

Inventory existing runner capabilities first. Select one additional useful backend after inspecting actual dedicated-access availability, rights and server compatibility. Pin reviewed upstream Agent Reach source/dependencies only if its CLI is necessary; installing it is not mandatory for extending the existing bridge.

Collectors run outside the web process. No arbitrary shell/URL execution, personal browser cookie extraction, host mounts, Docker socket or app database credential. Enforce destination/DNS/redirect controls, executable/argument allowlists, bounded bytes/time/processes and process-group teardown. A CLI can bypass application HTTP guards; isolation must enforce equivalent restrictions.

Normalize native ID, canonical permalink, publisher identity, source binding, publication/retrieval timestamps, retained payload hash/reference, backend version and justified topic/place association. Missing metrics remain null; mentions are not audience size. Deduplicate retries/cross-task records and propagate tombstones, restrictions and corrections.

Current candidate acceptance only permits eligible active RSS news sources. Do not disguise social results as RSS or loosen this path to bypass permissions. Adding direct social observations requires its own reviewed evidence-ingestion adapter. Collector output is a candidate, not publishable evidence or an instruction to alter targeting.

Readiness distinguishes configured credentials, dependency diagnostics, bounded real-fetch success, last success/freshness and quota/cost qualification. One backend pilot does not establish multi-platform coverage.

## 5. Commercial product contracts

### Surfaces and editorial independence

First proposed eligible surface: a clearly separate sponsor module on a topic landing page. Later: digest footer and separate search sponsor module. Protect assessment conclusions, evidence panels, disputed/withdrawn notices, severity labels, source credibility, essential controls and organic search order. Sponsors cannot suppress coverage. Finalize eligible/protected surfaces before rendering.

First release uses reviewed plain text and locally hosted reviewed images, validated stored destinations and visible Sponsored disclosure. No arbitrary HTML/JS creatives, fake sponsors or sample revenue claims. Disabled/expired/unfilled demand leaves useful content without an empty ad box. No sticky overlay initially.

First-party sponsorship need not add third-party tracking, but privacy wording must accurately describe sponsorship and measurement. Programmatic advertising is a separately controlled later adapter.

### Booking and money

Time intervals are UTC instants with explicit operator timezone display, active on [starts_at, ends_at). Specify scope/exclusivity per surface/topic/place and advertiser conflicts. Serialize overlapping exclusive bookings and recheck on activation. A draft proposal is not inventory reservation.

Store explicit currency and integer minor-unit agreed fees, never payable floats. First pilot is manually agreed flat fee with no impression guarantee. No automatic invoice, settlement, refunds, price edits or payment provider activation.

### Measurement and reporting

Separate eligible opportunity, server render, client-observed view/viewability, click, invalid/duplicate event and provider billable quantities. Each has metric version, denominator, period and attribution semantics. Server renders are not billable impressions. Missing/partial periods are unavailable, not zero.

Client telemetry remains spoofable observation. Bind events to server-issued booking/creative/placement revisions; validate payload/rate/time and atomic deduplication. Signed tokens prevent field tampering, not proof of human viewing. No PII, secrets or persistent reader profile in event IDs/tokens/URLs. Set explicit raw-event retention and aggregate rebuilding behavior.

Redirects use stored validated destinations, recheck pause/expiry and reject open redirect inputs. Imports reconcile provider account/period/unit/currency totals and explain variance; first-party observed totals cannot authorize payment.

### Contextual intelligence

Classify content, not readers. Initial taxonomy: energy/food, verified place scope, published content type, bounded canonical topic tags and suitability eligible/restricted/unknown. Content geography is not visitor location. Severity/sentiment alone do not prove suitability. Unknown/disputed/stale/withdrawn inputs cannot default to eligible.

Bind context to exact content/assessment revision/hash, taxonomy/classifier/prompt/model versions, evidence refs, expiry and decision reasons. Invalidate continuously after correction, withdrawal, permission or taxonomy changes. Keep commercial projections separate; never overwrite editorial models.

Deterministic matching uses curated advertiser categories and explainable reasons, not invented fit percentages. Package drafts use actual eligible inventory and known agreed fees; missing availability/prices remain unknown. Reports calculate facts deterministically; optional AI prose cites those facts and cannot invent traffic, forecasts or causal lift. Creative suggestions require operator approval.

## 6. Proposed schema and APIs

These are NEW proposed contracts, not existing imports. F03 finalizes exact fields/types, caller/transaction ownership, failure behavior and idempotency before coding. Use existing SQLAlchemy Base/naming conventions and Alembic/PostgreSQL patterns. Refresh migration head; do not assume a new revision number.

| Proposed entity | Required semantics |
|---|---|
| Sponsor | Public name/reviewed website/status, minimal private business contact. |
| Campaign | Sponsor FK, draft/approved/active/paused/ended lifecycle, revision, creator/approver, currency, optional minor-unit agreed fee/agreement ref. |
| PlacementBooking | Campaign, allowlisted surface/topic/place scope, interval, approved creative version, exclusivity policy and activation revision. |
| CreativeVersion | Immutable reviewed text/local asset/destination, approval decision; edits create a version. |
| DeliveryEvent | Metric/event version, booking/creative revision, dedup key, observation time and rejection/validity state; limited retention. |
| DeliveryAggregate | Unique bucket/placement/metric version; rebuildable totals and explicit incomplete periods. |
| ContentContext | Exact content revision/hash, taxonomy/classifier version, bounded tags/suitability/reasons/evidence refs, expiry/status. |
| CommercialDraft | Versioned match/package/report/creative proposal, immutable input refs/facts and operator decision; no auto-booking. |
| ProviderDeliveryImport | Provider/account/period/import identity, checksum, units/currency and reconciled totals. |

Proposed service names, to be typed/finalized in F03:

- eligible_placement(session, *, surface, topic, place_id, at) -> PlacementDecision
- record_delivery_event(session, *, envelope, received_at) -> EventReceipt
- classify_content(session, *, content_ref, taxonomy_version) -> ContextDecision
- match_sponsors(session, *, context_ref, at) -> list[MatchProposal]
- draft_package(session, operator, *, input_refs, expected_revision) -> CommercialDraft

Inputs use extra-forbidden Pydantic schemas. State whether each service flushes/commits; never perform network calls while holding booking/settings locks. Define safe error DTOs and immutable returned projections.

Proposed admin routes: /admin/commercial, /admin/sponsors, /admin/campaigns, /admin/commercial/context, /admin/commercial/reports. Combine views when clearer; no empty scaffolding. Reuse actual admin auth/CSRF/audit mechanisms. B04 defines public event/redirect paths after checking middleware, cache and abuse boundaries. No public request triggers a collector or LLM.

## 7. Dashboard and resource controls

Extend existing setting catalog and configuration coverage manifest. Supported functions need a working control or explicit code/deployment/human ownership; configurability does not permit arbitrary command/provider/targeting execution.

| Group | Required operator functionality |
|---|---|
| Acquisition | Existing global switch, reviewed backend switch/config, real-fetch readiness/freshness, next window, quotas, active tasks, review/cancel/reconcile. |
| Sponsorship | Global and surface switches, sponsors, campaign/creative approval, bookings, pause/dates/conflicts and audit. |
| Programmatic | Independent switch, validated account/slot IDs, consent readiness, health/unfilled/error states. |
| Context | Deterministic rebuild controls, taxonomy/invalidation/backlog; separate AI classifier switch and reviewed route. |
| AI resources | Existing global AI switch, daily/job caps, reserved/uncertain/settled spend, concurrency, timeout/attempt limits/windows. |
| Expensive commercial work | Persisted schedule, batch/concurrency bounds, next window, active jobs and admission reason. |
| Reports | Metric definitions, periods/availability, retention, export and reconciliation. |

Use expected revision checks, redacted audit and existing operator permissions. Deployment deny always wins. Show desired versus effective state with blocking reasons; an enabled switch does not imply a qualified capability.

Schedule collectors, model classification, bulk rebuilds, package generation and large exports. Suggested newly enabled pilot schedule: 01:00-04:00 Africa/Lagos, concurrency 1, small validated batch. Do not overwrite existing schedules. Register new job kinds in policy/admission mappings; queued, retry and manual paths all obey controls. Recheck before expensive dispatch and before accepting stale results. Preserve claim tokens/lease fencing.

Ad rendering and lightweight events remain available outside processing windows when enabled. Expiry/withdrawal, consent enforcement, invalidation, cancellation/reconciliation, deletion, security, backups and independent monitoring remain continuous. Closed windows cannot keep stale/expired campaigns or contexts visible.

## 8. Atomic implementation register

One ID per dispatch. Split further if unrelated behaviors emerge. Integration paths are families to inspect; each packet supplies exact allowed files/hashes/callers. New symbols are explicitly NEW. Tasks start Proposed; F00 is ready for read-only execution.

### F: foundation

| ID | Outcome | Depends on | Acceptance |
|---|---|---|---|
| F00 | Refreshed main/source/CI baseline | None | Actual status/hashes/divergence/tree diff/Alembic head and preserved handoff; no app edits. |
| F01 | Editorial/commercial invariant contract | F00 | Prewritten tests/policy: sponsorship cannot alter evidence, severity, publication, organic order or withdrawal. |
| F02 | Surface/privacy/security design | F00 | Eligible/protected surfaces and data map; native vs third-party behavior; no blanket CSP weakening. |
| F03 | Typed commercial contract | F01/F02 | Final schema/service DTOs, time/money/status/transaction/idempotency/authorization rules. |
| F04 | Commercial switches/admission | F03 | Existing settings/jobs/coverage extended, default off, revision/audit and registered job mappings verified. |

### A: acquisition

| ID | Outcome | Depends on | Integration and acceptance |
|---|---|---|---|
| A00 | Existing bridge gap/qualification inventory | F00 | Runner/ops/handlers inspected; no rebuilding working controls. |
| A01 | One backend protocol/isolation design | A00/F01 | Actual dedicated access path, pinned interfaces, auth/errors/egress/process/time/byte limits. |
| A02 | Offline adapter | A01 | NEW runner backend; malformed/partial/tombstone/rate-limit/timeout fixtures; no arbitrary execution. |
| A03 | Provenance persistence | A02 | Existing evidence/storage inventory then minimal additive binding; retry uniqueness, portable refs, rights/deletion. |
| A04 | Reviewed ingestion integration | A03 | Actual permission/pipeline contract; RSS acceptance remains narrow, no automatic source/publication. |
| A05 | Backend console/lifecycle | A04/F04 | Existing Reach UI/ops; switches/readiness/windows/quotas/cancel/uncertain reconcile and late-result fencing. |
| A06 | Backend acceptance packet | A05 | Repeatable staging checks; real fetch/egress/rights/quota/cost pending without environment. |

Repeat A01-A06 with explicit child IDs per additional backend.

### B: direct sponsorship

| ID | Outcome | Depends on | Integration and acceptance |
|---|---|---|---|
| B01 | Sponsor schema/service | F03 | NEW model/migration/service; minimum business data, status/URL/audit and fresh/upgrade DB tests. |
| B02 | Campaign/creative lifecycle | B01 | NEW approval/version services; immutable assets, authorized transitions, revision conflicts, pause/expiry. |
| B03 | Booking eligibility/conflicts | B02 | Scoped intervals/exclusivity; concurrent creation/activation, invalid context/revisions, no lock-held network. |
| B04 | Measurement contract | F03 | NEW typed event/token/rate/retention/redirect definition before collection. |
| B05 | Bounded event ingestion | B03/B04 | Atomic replay uniqueness, tamper/cross-placement/oversize/rate/concurrency, continuous cheap collection. |
| B06 | One topic sponsor module | B03/F02/F04 | Public template/local assets; disclosure, protected surfaces, off/expired/unfilled/browser/redirect tests. |
| B07 | Operator campaign console | B02/B03/F04 | NEW admin UI; auth/CSRF, approvals/conflicts, effective state/redacted audit. |
| B08 | Versioned report | B05 | Aggregates/periods/denominators, unavailable vs zero, replay/rebuild equivalence; private report first. |
| B09 | Native disclosure/privacy update | B04/B06 | Wording matches actual data/cookies/retention; draft source is not legal sign-off. |
| B10 | Direct pilot acceptance packet | B07/B08/B09 | Default-off local pilot and manual terms; test-only fixtures do not create real advertisers/prices. |

### C: contextual intelligence

| ID | Outcome | Depends on | Integration and acceptance |
|---|---|---|---|
| C01 | Taxonomy/context schema | F03 | NEW context DTO/model/migration; energy/food/canonical place/exact content revision/hash/version/unknown semantics. |
| C02 | Deterministic classifier | C01 | Published metadata -> separate projection; held-out labels, disputed/stale/unknown cases and no reader inference. |
| C03 | Continuous invalidation/scheduled rebuild | C02/F04 | Existing correction/withdrawal hooks/jobs; dedup/batch/fencing, never serve stale/withdrawn context. |
| C04 | Explainable matching | C02/B01 | NEW deterministic service with curated categories/eligibility/reasons; no invented probabilities. |
| C05 | Reviewed package drafts | C04/B03/B08 | Actual inventory/known fee, revision-bound approval; no auto-booking/outreach. |
| C06 | Optional AI purpose/schema | C01/F04 | Existing config/catalog plus NEW prompt/schema; reviewed route, bounded output/deterministic fallback. |
| C07 | Budgeted classifier job | C03/C06 | Existing adapter/queue; transfer permissions, reservations/caps/uncertain calls, closed-window/retry/manual/stale-result tests. |
| C08 | Optional report prose | C05/C07/B08 | Deterministic facts first, fact-linked prose/error fallback; no invented traffic/lift/forecast. |
| C09 | Creative draft suggestions | C05/C07/B02 | Operator review, immutable approved asset/destination; no public model scripts or auto-activation. |

C06-C09 are optional after a usable deterministic pilot.

### P: optional programmatic delivery

| ID | Outcome | Depends on | Integration and acceptance |
|---|---|---|---|
| P01 | Provider/consent/account contract | F02/B04/C02 | Current official selected-provider docs, actual account eligibility, slots/data map/consent/origins. |
| P02 | Default-off config/slot registry | P01/F04 | Validated identifiers and reviewed placements; examples cannot prove readiness. |
| P03 | Consent-gated browser adapter | P02/B06 | Load once when eligible; denied/unknown/off no unauthorized requests, offline/adblock/unfilled/timeout/mobile. |
| P04 | Narrow CSP/privacy change | P03/B09 | Necessary public origins only; preserve strict admin CSP; no blanket wildcard/unsafe-eval. |
| P05 | Context-to-targeting mapping | P03/C03 | Allowlisted bounded current keys, no PII/raw queries/sensitive traits, withdrawal/cache tests. |
| P06 | Provider reconciliation | P01/B08 | Immutable idempotent imports, units/currency/timezone/variance; observed events not billing authority. |
| P07 | Programmatic acceptance packet | P04/P05/P06 | Real account/consent/browser/request/reconciliation checks prepared; default-off and plan-only staging. |

### Q: verification and closure

| ID | Outcome | Depends on | Exit |
|---|---|---|---|
| Q01 | Migration/concurrency review | Changed schema/admission work | Disposable PostGIS fresh/upgrade/constraints plus booking/event/budget races; failure-oriented independent review. |
| Q02 | Dashboard/browser acceptance | Delivered UI | Desktop/mobile/keyboard/screen-reader, CSRF/roles/save conflicts, controlled-clock expiry/windows, consent/layout. |
| Q03 | Linux milestone checks | Delivered source slice | Exact commit/digest, CI, images as changed, restore drill, explicit skips/failure disposition. |
| Q04 | Repeatable staging plan | Enabled-scope packets | Host/accounts/secrets/egress/resources/test recipients/publication suspension/rollback/monitoring/drills; no deployment. |
| Q05 | External acceptance register | Q04 | Rights/privacy/editorial/provider/metering evidence or named pending owner; fixtures cannot close it. |
| Q06 | Release dossier | Q01-Q05 | Enabled-capability blockers resolved, independent editorial/security/financial review and concrete rollback/release decision. |

## 9. Validation and evidence

Inspect fixtures and resolved environment before tests/migrations/seeding/restore. tests/conftest.py uses TEST_DATABASE_URL; integration fixtures drop schema. Use dedicated disposable PostgreSQL/PostGIS, isolated storage and test credentials. SQLite cannot validate these locks, JSONB, PostGIS or queue semantics.

Current CI: Python 3.12, locked dependencies, pip check, ruff check ., ruff format --check ., mypy src, pytest, disposable restore drill, pip-audit and app/backup builds. Copy actual executable commands/configuration from current .github/workflows/ci.yml. Build runner when changed. Network boundary tests require RUN_NETWORK_TESTS=1 and network; disabled tests are skips. Historical counts are not targets that justify deleting failures.

Failure checks: global/specific disable; closed-window queued/manual/retry admission; setting race; worker death/reclaim; uncertain provider result; budget race/restart; late output after withdrawal; overlapping booking race; replay/tampered events; open redirects; consent unknown/denied; stale context; provider outage; empty/off page; private report access.

For schema/security/financial/concurrency contracts use a reviewer distinct from implementation where available. If one model does both passes, record reduced independence and preserve prewritten invariant probes. Do not weaken tests for green CI. Browser/runtime claims require actual evidence.

Task state: Proposed -> In progress -> Source review -> Source accepted -> Runtime qualified (or N/A with reason). Record task/base/result commits, files/diff, actual commands/exit codes/counts/skips, contract/data impact, limitations and rollback. Whole-project source acceptance cannot close external activation gates.

## 10. Staging, rollback and existing readiness

Prepare instructions only until an authorized staging host/accounts are configured. Prerequisites: dedicated test recipients/cohort, isolated DB/object storage, limited provider credentials, network egress, CPU/RAM/process ceilings, TLS/auth, independent alerts and retention. Collector has no application DB/publication credentials.

Use exact image digest with publication suspended. Separate source-fetch qualification from editorial approval. Prove disabled/unfilled/expired/withdrawn behavior using explicit test campaigns; test consent before provider calls; compare measured/provider quantities over a known period. Rehearse kill switches, worker restart/uncertain cancellation, portable DB/evidence restore and deletion propagation. Q04 defines observation duration/criteria; do not inherit TV Insights' 14-day period as an unstated AfricaSignal requirement.

Rollback: disable affected capability first, stop admission, invalidate public commercial projections and keep ordinary pages useful. Retain uncertain remote reservations until termination/reconciliation, preserving audit/evidence. Revert scoped compatible code/config; prefer additive schema compatibility/forward fixes to blind live downgrade/drop. Record unknown dispatch/billing separately.

Existing ASR launch gates still apply to enabled capabilities: editorial quality, source rights/coverage, legal/privacy, browser/accessibility/indexing, real backup/deletion recovery, independent monitoring and on-call drills. Revenue features do not supersede them.

## 11. Later parity and reserved decisions

| Later capability | Prerequisite |
|---|---|
| More social sources | Repeat reviewed A chain per backend; actual rights/access/egress. |
| Sponsored search | Separate paid module, unchanged organic order, no raw query targeting. |
| Digest/report sponsor | Existing delivery contracts first; disclosure in every format and truthful delivery denominator. |
| Sponsored trend/index | Verified denominator/coverage/freshness; metric unaffected by payment. |
| Sticky/mobile | Close/keyboard/viewport/provider acceptance; content unobstructed. |
| Advertiser discovery | Public-business provenance, reviewed candidates; no automatic contact harvesting/outreach. |
| Behavioral segments | Separate lawful collection/consent/retention/deletion design, no sensitive inference and aggregate-size controls. |
| Forecasts | Representative history, out-of-time baselines, uncertainty/coverage/unavailable state. |
| Yield/price automation | Trusted units, pricing decision, shadow evaluation, bounded holdout/rollback. |
| Payments/self-service | Separate ledger/ownership/idempotency/refund/reconciliation/provider project. |
| Cross-product exchange | Separate versioned HTTP contracts/ownership; no shared DB or assumed Aether integration. |

Reserved decisions block dependent activation only: real advertisers/terms/fees, chosen ad account/provider, consent/geographies, collector credentials/rights and staging host. Offline deterministic source work can proceed once implementation is authorized. Never ask for secrets pasted in chat.

## 12. Model choice and first action

Both requested coding models use the same packets and acceptance requirements. The coding model is separate from the application's runtime provider; using Gemini for coding does not require a Gemini SDK/route in AfricaSignal.

Official references checked 2 October 2026: [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) and [Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/latest-model?hl=en). Verify account/tool access at dispatch. No comparative AfricaSignal benchmark was run.

GPT-6 Luna is a practical default in the existing Codex workflow; Gemini 3.8 Flash is a viable alternative when its environment can inspect/apply diffs and run the same checks. This is an integration recommendation, not a measured coding-quality ranking. Keep senior review for schema/budgets/booking/security/measurement. Neither a model name nor small packets guarantees error-free code.

If selecting empirically, run identical narrow pilot packets against the same baseline in disposable branches. Compare accepted correctness, unrelated diff, repairs, actual time and billed cost; accept only one reviewed implementation. Do not merge competing patches blindly.

Next action: provide both documents and the companion bootstrap prompt; dispatch F00 only. Finalize F01-F03 before commercial schema. The full register is not a single batch implementation prompt.
