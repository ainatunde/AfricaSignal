# AfricaSignal production-readiness audit and remediation register

Date: 1 October 2026 (Africa/Lagos)
Repository: ainatunde/AfricaSignal
Baseline audited revision: cfd47473914b5ff209a987aaff27930e11c40623 (origin/main)
Remediation branch: codex/production-readiness-remediation (source and Linux verification recorded below)
Recommendation: DO NOT LAUNCH YET. Proceed with engineering remediation and an isolated staging qualification.

## Scope and evidence limits

Reviewed application configuration, Docker/Compose, CI, auth and settings, email/outbox, workers and scheduling, model adapter/budgets/evaluation, fetching/robots, evidence storage/permissions/retention, publication/invalidation, public/API/channel output, privacy deletion/recovery, backups, monitoring and launch documents. The baseline audit used database-free probes; remediation was then exercised on Linux against an isolated PostgreSQL/PostGIS 16 database and in the complete suite. This is not a penetration test or live-production certification. No deployment address, infrastructure account or production settings were supplied, so deployment-specific evidence remains unverified. The database run validates regressions in this branch but does not replace staging acceptance for external-provider races, cross-channel behavior, or operational recovery.

Remediation-branch verification (Linux, 1 October 2026):
- `pip check`: passed; Ruff lint and format: passed (323 files); mypy: passed (158 source files).
- Full suite against the isolated PostGIS 16.3.4 test database with `RUN_NETWORK_TESTS=1`: 2,053 passed, 4 skipped, 668.76 seconds.
- Alembic upgrade to schema `0034`: passed; source seed created 16 sources and 16 permissions.
- Fresh PostgreSQL 16 backup and restore drill: passed. Checksum verified, schema `0034` restored, 35 tables compared, sequences and contents matched; restore completed in 3 seconds.
- `pip-audit`: no known vulnerabilities found for auditable dependencies. It skipped the private `africasignal==0.1.0` distribution because that package is not published on PyPI.
- Production application image and backup image both built successfully; each image's `pip check` passed. The backup image contains PostgreSQL 16.15 client tools.
- These are local Linux and disposable-database results. They do not verify a live deployment, real object-storage recovery, production mail/model delivery, browser acceptance, or operator response.

## Confirmed defects and source-supported risks

The evidence and defect statements in this section describe the baseline revision above. Current branch dispositions and revalidation evidence are recorded below.

### ASR-01 — High — Outbox batch locks are released before the batch finishes

Evidence: src/africasignal/publish/outbox.py:98 selects up to 50 rows FOR UPDATE SKIP LOCKED and commits after each row (:124). The first commit releases all selected locks, while the dispatcher retains the remaining row objects. The scheduler can queue overlapping dispatcher jobs and multiple workers can claim them. With expire_on_commit=False, the first worker can also retain stale cancellation/status values.

Consequence: duplicate sends or sends of rows another worker has already sent/cancelled. Provider idempotency is an additional protection, not a replacement for queue ownership.

Remediation: claim one message in each transaction, or persist a sending state, claim token and lease before releasing a batch. Revalidate ownership/status before each send, use conditional completion, and preserve the exact idempotency key. Avoid holding an entire batch's locks across network calls.
Acceptance: two dispatchers plus a barrier after the first commit; each row is owned once. Add crash-after-provider-acceptance and cancellation-interleaving cases on real PostgreSQL.
Owner: backend engineering.

### ASR-02 — High — Consent and version eligibility are incomplete at send time

Evidence: outbox.py:75 checks digest_opt_in for digests, but not corrections. A probe rendered a correction for digest_opt_in=False. versions.py:202 cancellation updates only pending outbox rows, omitting failed rows. notify.py:cancel_superseded later cancels both, but runs asynchronously. Email payloads are snapshots, and dispatch does not inspect assessment_version_id eligibility. Login payloads have links but no dispatch-time expiry check.

Consequence: emails after unsubscribe, obsolete corrections in the asynchronous cancellation gap, and delivery of already-expired sign-in links after an outage.

Remediation: one send-time eligibility function for consent, account state, current/relevant assessment version, explicit withdrawal notices, notification cancellation and message expiry. Cancel pending AND failed rows in the same publication transaction. Store token/message expiry metadata; drop expired login mail and scrub the link. Give sign-in delivery an explicit latency target.
Acceptance: enqueue then unsubscribe, supersede, withdraw, delete account or expire token; no ineligible email reaches the provider. Preserve a legitimate withdrawal notice.
Owner: backend engineering.

### ASR-03 — High — Publication suspension is checked only once per email batch

Evidence: outbox.py:dispatch_pending reads publication_suspended before selecting rows; it is not reread before later provider calls. ASR-01 makes stale state worse. The site intentionally permits bounded public-page caching after suspension.

Consequence: a suspension made during dispatch can leave later notifications sending. This needs a defined boundary because an already-started provider request cannot be recalled.

Remediation: check current suspension immediately before every notification send; coordinate claims with a publication generation/state, and cancel or hold claimed rows when suspension changes. Document in-flight-send and cache limits. Keep login mail exempt deliberately.
Acceptance: suspend between two sends, prove later unstarted sends stop; verify all workers and caches in staging and production.
Owner: backend engineering and operations.

### ASR-04 — High — Production email can report false success; retry/idempotency handling is incomplete

Evidence: email.py:build_provider_named accepts fake/console; get_provider does not refuse them outside development. Environment fallback bypasses console choices. A production probe selected FakeProvider. Postmark/Resend return empty message IDs for successful JSON lacking the expected identifier, which outbox marks sent. _classify treats all 409 responses as permanent errors. The Postmark adapter sends no provider idempotency key; Resend does.

Remediation: enforce a production provider allow-list at resolution; require validated provider acceptance IDs/schema. Classify Resend concurrent_idempotent_requests as retryable and payload mismatch as terminal. Freeze the outbound payload across retries. Choose/document duplicate handling for Postmark; do not claim provider-independent exactly-once delivery. Reconcile ambiguous provider outcomes and delivery/bounce events.
Acceptance: fake/console rejected in staging/production; malformed 2xx cannot become sent; concurrent 409 retried; crash/retry works within the selected provider's guarantees.
Resend documents a 24-hour idempotency window: https://resend.com/docs/dashboard/emails/idempotency-keys
Owner: backend engineering.

### ASR-05 — High — Full-text storage permission does not govern raw objects

Evidence: evidence/capture.py:133-135 stores every raw document. :149 suppresses only the extracted text_content column when may_store_full_text=False. load_text(:163) reconstructs the complete text from the raw object.

Consequence: the database looks compliant while complete articles remain in object storage and backups. The permission flag's meaning is inconsistent with its implementation.

Remediation: define separate explicit rights for temporary processing, retaining raw documents, retaining full text, quotations and model-provider disclosure. When retention is forbidden, process ephemerally and retain only permitted provenance/hash/excerpts/figures. Purge existing disallowed raw copies and apply the policy to backups/caches. Do not merely rename the flag.
Acceptance: a collect-but-no-retention permission leaves no full article in database, app bucket, backups or model cache after processing.
Owner: backend engineering plus source-rights owner.

### ASR-06 — High — Evidence retention deadlines are never enforced

Evidence: capture.py:152 sets retention_until, but repository search found no consumer that expires evidence on that deadline. publish/retention.py covers accounts, tokens, feedback and deletion ledgers only. Backup object copying is copy-only.

Remediation: implement an idempotent retention job that expires permitted text/raw objects, preserves minimal allowed provenance, handles shared content-addressed keys safely, and invalidates or adjusts dependent assessments as appropriate. Apply explicit policies to claim passages, cached model outputs, uploads and backup copies.
Acceptance: time-bound source data disappears at the deadline in every relevant store; shared objects needed under another valid permission are not accidentally deleted; retries are safe.
Owner: backend engineering and operations.

### ASR-07 — High — Recovery can resurrect deleted accounts or silently omit deletions

Evidence: restore.sh ends with restore ok but does not invoke deletion replay; runbook.md recovery/drill instructions omit it, while data-protection.md requires it separately. admin.py:105 warns and returns success without external storage. deletions.py:91 skips malformed ledger objects. Mirroring is asynchronous and may complete a job with no storage; a daily pass is the fallback. data-protection.md explicitly acknowledges lost recent deletions on simultaneous bucket loss and unreplayed non-account deletions. Copy-only backups retain ledger objects after app pruning.

Remediation: make recovery a gated workflow: freeze web/worker/scheduler, restore, load the current independent deletion ledger, fail closed on missing/unreadable entries, reapply all supported erasures, verify, then enable traffic. Store the authoritative ledger durably in an independent failure domain, acknowledge its durability before treating deletion as recoverably complete, and reconcile ledger retention with actual recoverable backups. Include non-account erasures or disclose and approve a manual procedure.
Acceptance: delete after a backup, lose database and app bucket, restore; deleted data remains deleted. Corrupt/unavailable ledger blocks readiness, and replay uses the explicitly named restored database.
Owner: backend engineering, operations and privacy owner.

### ASR-08 — High — Production configuration accepts weak keys and unvalidated environment fallbacks

Evidence: config.py:52 validates only non-empty DATABASE_URL and SECRET_KEY. SECRET_KEY=x passes. The same key protects operator TOTP secrets, saved credentials, signed tokens and deletion fingerprints. settings_store.py:resolve returns environment values without normalise, so console constraints are not universal. public URL/provider values can therefore be unsafe or invalid through environment configuration.

Remediation: validate every resolved setting regardless of source; reject known development secrets and insufficient key strength, require an approved HTTPS public origin, validate database/storage/provider values, and expose actionable preflight failures without secret contents. Introduce versioned keys and a rotation/re-encryption procedure that preserves deletion fingerprints across rotations; separately escrow recovery keys.
Acceptance: weak/default secrets, non-HTTPS public origins and fake providers fail preflight; valid setup passes; rotation and restored encrypted settings work in a drill.
Owner: backend engineering and operations.

### ASR-09 — High — Robots failures permit crawling; source pacing and preflight deadlines are incomplete

Evidence: net/netutil.py:40 treats any >=400 robots response or exception as no robots/allow. It does not correctly follow robots redirects. politeness uses can_fetch and per-process buckets, but no consumer of crawl_delay/request_rate was found despite recorded 10/30-second source delays. fetch.py starts its whole-fetch timer after the initial robots/politeness gate; the robots body has only per-operation timeout and a size limit.

Remediation: distinguish 4xx unavailable from 5xx/network unreachable, retain last valid policy, follow bounded SSRF-safe robots redirects, and abstain on unreachable policy. Honor configured publisher pacing, including delays, across workers. Extend a hard end-to-end deadline through DNS/robots/body and isolate expensive PDF/HTML parsing with resource/time limits.
Acceptance: robots 503/timeout disallows, safe redirect loads policy, forbidden destination is refused, two workers obey the same pace, and a trickle-response robots server cannot hold a worker indefinitely.
RFC 9309 requires complete disallow for unreachable robots: https://www.rfc-editor.org/rfc/rfc9309.html
Owner: ingestion engineering.

### ASR-10 — High — Assessment updates do not serialize against concurrent publication/withdrawal

Evidence: publish/situations.py:409 loads Situation without FOR UPDATE and allocates latest.version + 1. policy_situations.py follows the same pattern. AssessmentVersion has a unique (situation_id, version) constraint, but worker jobs with different dedupe keys may target the same situation. Operator withdrawal locks Situation; the assessor does not acquire that lock before reading its state.

Risk: version collisions/retries and stale computation overwriting a concurrent correction or withdrawal. This is a source-supported concurrency risk; no PostgreSQL reproduction was run locally.

Remediation: serialize per situation with a row/advisory lock covering input snapshot, version allocation, policy and pointer update, or use optimistic generation checks with recomputation. Define how automatic reassessment treats an explicit operator withdrawal so it cannot silently undo it.
Acceptance: concurrent assess/assess, assess/correction and assess/operator-withdraw scenarios; monotonic versions, current pointers and operator decisions remain correct.
Owner: backend engineering.

### ASR-11 — High — Integration tests can destroy a wrongly configured database

Evidence: tests/conftest.py uses setdefault for DATABASE_URL and ENV. tests/integration/conftest.py:20 runs alembic downgrade base before upgrading and again on teardown. An inherited application DATABASE_URL therefore survives the test defaults. The evaluation harness has a database-name safety check; the integration fixture does not.

Remediation: refuse all integration execution unless an explicit isolated test database is verified; validate environment, host/name and a test marker before the first migration. Use a dedicated role with no privileges on application databases and provision a disposable database per run.
Acceptance: a normal development/staging/production URL is refused before any connection/migration; only the isolated test target can be reset.
Owner: engineering/CI.

### ASR-12 — Medium — Lease loss does not promptly stop work or fence intermediate effects

Evidence: worker.py heartbeat ignores the boolean returned by extend_lease. queue.complete checks status/owner, but not lease_until. LlmAdapter commits the caller session for accounting/cache, and explain_backfill commits per version before Worker completes the job. import_nbs_file deletes the temporary upload before the transaction that records import success commits.

Risk: a worker continues after losing ownership; rollback at completion cannot undo provider calls, earlier commits or object deletions. An import retry may find its upload missing after a rollback/crash.

Remediation: propagate lease-loss cancellation; use a unique attempt/fencing token and ownership checks for writes. Persist model accounting in its own session. Make upload cleanup a post-commit idempotent job. Identify every irreversible external effect and provide a retry/reconciliation contract.
Acceptance: force lease loss mid-handler and crash after object cleanup; old attempts cannot publish/overwrite results and import retries retain recoverable inputs.
Owner: backend engineering.

### ASR-13 — Medium — Spending limits are soft and total-token limits omit the next input

Evidence: llm/budget.py deliberately checks spend without reservations. adapter.py:163 subtracts prior input+output tokens, then limits only the next output max_tokens; it does not reserve/count the next prompt's input before sending. A billed response whose recording transaction fails can also escape accounting.

Remediation: label the daily amount as a soft threshold if accepting bounded overshoot, cap worker concurrency and provider-side spend, and reconcile actual bills. For a hard cap, reserve worst-case input/output cost in a short transaction and settle afterward. Account for input tokens in the per-job allowance and record ambiguous calls.
Acceptance: concurrent calls near cap and input larger than remaining allowance; measured overshoot stays within the explicitly accepted bound.
The configured Sonnet 5.5 base price matches Anthropic's current $2 input/$10 output per million tokens: https://www.anthropic.com/claude-sonnet-5-5
Owner: backend engineering/product budget owner.

### ASR-14 — Medium — Proxy trust and rate limits depend on deployment topology

Evidence: client_address.py:72 trusts X-Forwarded-For when a hop count is configured without checking the connecting peer against a proxy allow-list. web/ratelimit.py and net/politeness.py are in-process. Compose exposes web on port 8000. Actual production firewall/proxy configuration is unverified.

Remediation: allow connections only from the trusted edge or validate trusted proxy CIDRs; strip/append forwarded headers consistently and coordinate Uvicorn's proxy handling. Use shared expiring limits for multiple processes/replicas or impose matching edge limits; bound limiter memory. Retain the documented privacy approach.
Acceptance: direct forged headers cannot bypass limits; legitimate carrier/proxy users are identified correctly; two replicas share the intended quota.
Owner: operations and backend engineering.

### ASR-15 — Medium — Freshness is not presented consistently across output channels

Evidence: public templates prefix stale price headlines, but api_v1.py:166 returns stored headline unchanged. digest.py uses published status/recent publication without valid_until at generation and retains snapshot content at send. whatsapp_text.py:117 likewise lacks a valid_until predicate. The latest PR explicitly left API/email text unchanged.

Remediation: define one freshness/presentation contract for HTML, API, digest and channel drafts; carry measurement period, validity and effective status explicitly. Rebuild or filter queued content at send, and reject old channel drafts when recording/posting.
Acceptance: an expired assessment never reads as current news on any channel, including when the expiry scheduler is late.
Owner: backend engineering and editorial/product owner.

### ASR-16 — High — Monitoring can miss service failure

Evidence: web/app.py:38 returns HTTP 200 when database_is_up=False (confirmed by probe). /healthz checks only DB connectivity. health_alerts.py and backup_alerts.py write console/log alerts and explicitly have no operator notification channel. Scheduler failure prevents those alerts from running. Compose has no web/worker/scheduler restart policy or independent worker/scheduler healthcheck.

Remediation: separate liveness/readiness, return 503 for unready state, include schema/setup readiness, and add independent heartbeats for scheduler, worker and backups. Route actionable alerts to an on-call channel and monitor oldest queued/outbox age, retention/ledger lag and source freshness. Configure process supervision and graceful shutdown budgets.
Acceptance: kill each process, break DB/storage, disable scheduler or stop mail; an external monitor alerts within the agreed SLA and restart/recovery works.
Owner: operations.

### ASR-17 — Medium — Growth and retention of operational tables are unqualified

Evidence: periodic jobs create permanent slot dedupe keys; no job/outbox/model-call/cache cleanup policy was found. Several account/digest/channel queries load full result sets before processing/paging. No load-test or database maintenance results are in the audited evidence.

Remediation: define archival/retention for jobs, outbox, model-call/cache and audit data while preserving dedupe/recovery invariants. Page batches in SQL, measure query plans and indexes, set pool/connection/query timeout and CPU/memory/disk budgets, and tune vacuum/backup duration.
Acceptance: realistic launch-volume and growth tests with stated latency, queue drain, disk and recovery targets; no duplicate work after archival.
Owner: backend engineering and operations.

## Unverified launch gates and release controls

### ASR-18 — High — Real model quality has not been qualified

Evidence: eval/README.md:25 says the set is synthetic with stand-in replies; eval/cases contains only t1_seed.jsonl and t2_seed.jsonl. anthropic_provider.py explicitly says no live API check.

Remediation: editorial labels for at least 60 T1 and 40 T2 cases, real/recorded provider answers, locked train/test origin separation and an exact-model/prompt report. Cover hallucinations, ambiguity, reversals, stale material and difficult OCR. Preserve synthetic regression tests.
Acceptance: number accuracy 100%, zero scope more precise than evidence, T1 evidence-state >=90%, T2 >=80%, with editorial sign-off and failures/abstentions reported.
Owner: editorial owner and model engineering.

### ASR-19 — High — Production/staging deployment and disaster recovery remain unverified

Evidence: supplied Compose pins ENV=development and development credentials. runbook.md:250 records only a local-directory developer drill, explicitly not a staging bucket/encryption/app drill. No deployment URL or live infrastructure was supplied.

Remediation: create a production deployment configuration for pinned release image, HTTPS/domain, isolated DB roles/network, secure private buckets, independent encrypted backups, secrets escrow, migrations, worker/scheduler supervision and place loading. Build/test the actual image. Run recovery from real storage plus deletion replay and verify a second app instance.
Acceptance: measured recovery point <=24 h and time <=2 h per runbook; hash/content verification of restored objects, all critical pages and jobs work, deleted accounts remain erased; document rollback and a failed-migration recovery.
Owner: operations.

### ASR-20 — High — Live email, provider acceptance and abuse response remain unverified

Evidence: email.py says Postmark/Resend tested only with mocked transport; no real provider result or sender-domain acceptance record supplied.

Remediation: verify sender domain (SPF/DKIM and an appropriate DMARC policy), transactional/bulk routing and live provider settings. Exercise scanner-safe login, timely delivery, correction, weekly digest, unsubscribe, timeout/retry, bounce/complaint suppression and credential rotation.
Acceptance: real received messages and unsubscribe results with recorded timestamps, exact provider IDs, suppression behavior and no bearer tokens in logs.
Owner: operations and backend engineering.

### ASR-21 — High — Launch data coverage and source rights need acceptance

Evidence: launch.md P1; source seed comments record unreviewed terms, inactive outlets and unresolved NMDPRA route. News corroboration requires active approved sources and 30 days since first approval. The current live database/source approval state is unknown.

Remediation: approve only rights-reviewed sources with owner, permitted uses, working route, freshness expectations and review dates; record excluded coverage honestly. Do not shorten the 30-day trust rule to force launch. Validate actual NBS/NERC imports and parser results, with range review and publication through to public output.
Acceptance: each advertised topic/location has verified usable evidence or an explicit unavailable label; source permissions and coverage page agree; no synthetic or unlicensed gap-filling.
Owner: source-rights owner, editorial owner and ingestion engineering.

### ASR-22 — High — Legal/privacy and launch accountability are unfinished in repository evidence

Evidence: legal templates still contain todo(...) placeholders; data-protection.md has blank sign-off and NDPC answers. launch.md leaves operator identity/contact, transfer/provider details, cookie basis, retention choices, licences and legal review to owners.

Remediation: counsel review against the current Nigeria Data Protection Act and GAID; settle the actual decisions, provider contracts/countries, DPIA, cookie handling, erasure and recovery policies. Implement consent controls if that is the chosen lawful design. Fill identity/contact, then set legal_review_confirmed only after approval. Name the on-call operator and maintain takedown/privacy/incident procedures.
Acceptance: no unresolved legal template markers; signed decision record and matched runtime behavior. This audit does not decide registration or lawful basis for the business.
Current primary directive: https://ndpc.gov.ng/wp-content/uploads/2025/07/NDP-ACT-GAID-2025-MARCH-20TH.pdf
Owner: Tunde and qualified counsel.

### ASR-23 — Medium — Release reproducibility and enforcement need strengthening

Evidence: Python dependencies use broad ranges, Docker uses mutable base tags and there is no audited lockfile. CI runs lint/type/tests/restore drill/audit but does not build or qualify the deployable image. Classic main branch protection returned 404 and the effective branch-rules endpoint returned zero active rules: main has no enforced checks/review rules in the inspected GitHub configuration. Local bootstrap pip 24.0 has advisory entries.

Remediation: lock/hash dependencies and pin container digests, update/bootstrap pip to a patched supported release (advisories list fixes through 26.2), scan the actual image and retain its SBOM/provenance. Require status checks through branch rules/rulesets, review for sensitive changes, staged image acceptance and controlled promotion. Reconcile stale launch checklist statuses with source/runtime evidence.
Acceptance: reproducible image with clean relevant scans, required checks cannot be bypassed accidentally, and the tested digest is the deployed digest.
Owner: engineering/operations.

### ASR-24 — Medium/Low — Browser, accessibility and indexing decisions lack final acceptance

Evidence: prior PRs describe a browser pass, but no live staging browser record was supplied. Console tables intentionally scroll on phones; robots.txt/sitemap are absent by launch indexing decision. These are not automatically missing product features.

Remediation: browser acceptance of full reader/admin journeys on phone and desktop, keyboard/focus/screen-reader contrast and errors, slow network and pagination. Decide indexing policy; implement robots/sitemap/canonicals where discovery is desired, keeping private/admin pages excluded. Validate manual channel posting as the current product contract; automatic posting is a separate scope decision.
Acceptance: dated flow/accessibility results, understandable empty/stale/withdrawn/error states, no exposed private pages in indexing, and explicit indexing/manual-channel decisions.
Owner: product/frontend and operations.

## Capabilities already present in the baseline (context only)

- Database-backed operator sessions, password/TOTP auth, roles, origin guards and security headers exist.
- Source approvals and outlet ownership gating exist; approval queues an initial fetch.
- Reader scanner-safe one-time login, account export/deletion, token/session retention and feedback scrubbing exist.
- Jobs use SKIP LOCKED and leases; this is partial resilience, with ASR-01/10/12 remaining.
- Publication holds, superseded-hold checks, insufficient evidence handling and withdrawal presentation exist.
- Backups, encrypted dump enforcement outside development, restore/drill tooling and console alerts exist.
- GeoNames place loading, operator pages and paging, NBS file checks and XML entity protection exist.
- Stale HTML headline fixes and current merged CI are verified. They do not close the cross-channel or runtime gates.

## Remediation branch disposition and launch gates

The source changes close several code defects, but source completion and local Linux verification do not equal production acceptance. Classifications below describe this branch after the verification above.

| Gap | Current disposition | Remaining acceptance |
|---|---|---|
| ASR-01 | Addressed in source; per-row locking and completion are covered by the Linux suite. | Provider-acceptance crash and external delivery reconciliation still need staging evidence. |
| ASR-02 | Addressed in source; consent, current-version eligibility, and login-token expiry are checked at send time. | Real mail-provider behavior and bounce/complaint handling remain unverified. |
| ASR-03 | Partially addressed; suspension is rechecked between sends. | An already-started provider request cannot be recalled; qualify the boundary in staging. |
| ASR-04 | Partially addressed; fake providers fail closed, response IDs are validated, and Resend conflict handling is classified. | Live provider acceptance and Postmark duplicate/reconciliation behavior remain open. |
| ASR-05 | Partially addressed; source retention still follows the excerpt policy, while evidence/deletion backup prefixes mirror current state with checksum-based sync and content verification. | Validate rights, external caches, and every storage tier with accountable source owners. |
| ASR-06 | Partially addressed; backup mirrors remove expired evidence and retired deletion objects and verify mirrored content. | Qualify shared-object behavior and retention/deletion across every real storage tier. |
| ASR-07 | Partially addressed; restore remains quarantined through deletion replay, and current evidence/deletion prefixes are mirrored to backup. | Independent ledger durability, non-account erasures, bucket-loss recovery, and the real restore drill remain open. |
| ASR-08 | Partially addressed; production backup encryption and a protected-file passphrase input are supported in the staging plan. | Validate HTTPS origin, key rotation, escrow, and recovery with the accountable operator. |
| ASR-09 | Partially addressed; robots failures fail closed, Crawl-delay/Request-rate are applied, and robots/request/pacing work uses the remaining fetch deadline. | Synchronous OS DNS resolution can still overrun; cross-replica pacing remains process-local; production-like crawl acceptance remains open. |
| ASR-10 | Partially addressed; assessments serialize on the situation row and publication/withdrawal paths share that lock. | Record controlled concurrent assess/correction/withdraw acceptance against PostgreSQL. |
| ASR-11 | Addressed in source and CI; integration tests require an explicit isolated test database and refuse non-test targets before reset. | Keep the test role isolated in hosted CI and any developer setup. |
| ASR-12 | Partially addressed; each claim has a UUID attempt fence, and stale attempts cannot heartbeat, complete, or fail a reclaimed job. | Fence and exercise every intermediate write and irreversible effect under worker-crash/lease-loss scenarios. |
| ASR-13 | Partially addressed; worst-case input/output cost and per-job token reservations are admitted before provider calls; ambiguous outcomes remain reserved. | Configured price/token bounds need provider invoice reconciliation and an approved spend limit in staging. |
| ASR-14 | Partially addressed; forwarded addresses are trusted only from configured proxy CIDRs and Uvicorn proxy headers are disabled. | Shared rate limits, edge policy, and real topology validation remain open. |
| ASR-15 | Partially addressed; stale API headlines are labeled and queued email/channel outputs recheck current versions. | Cross-channel freshness behavior and manual-post acceptance need product/editorial sign-off. |
| ASR-16 | Partially addressed; worker, scheduler, and backup support independent heartbeat endpoints, and staging documents require distinct checks. | Configure real monitors and notification/on-call routing; drill process, DB, storage, and mail failures. |
| ASR-17 | Open; no realistic growth/load qualification or operational-table archival policy is included. | Define archival without breaking dedupe, then record query, queue, disk, and recovery targets. |
| ASR-18 | Open launch gate; evaluation data is still synthetic. | Complete the editorially labeled real/recorded provider evaluation and sign-off. |
| ASR-19 | Open launch gate; a repeatable Compose staging plan, startup publication hold, and recipient allowlist are prepared. | No staging host was supplied or deployment authorized; qualify real storage, backup/deletion replay, rollback, RPO, and RTO. |
| ASR-20 | Open launch gate; staging blocks sends to non-allowlisted addresses and scrubs sign-in links for rejected recipients. | Verify the sender domain, real provider delivery, unsubscribe/suppression, retries, and credential rotation. |
| ASR-21 | Open launch gate; live source approvals and advertised coverage are unknown. | Obtain rights/source-owner approval and validate live imports and output coverage. |
| ASR-22 | Open launch gate; legal templates and privacy decisions still need qualified review and sign-off. | Resolve the recorded legal/privacy TODOs and confirm runtime behavior with counsel and the accountable operator. |
| ASR-23 | Partially addressed; runtime/dev lockfiles and digest-only staging image references are present; the locked runtime dependency scan is clean. | Build and scan exact release images, retain SBOM/provenance, enforce repository rules, and accept deployed digests. |
| ASR-24 | Open; the acceptance record now captures browser/accessibility/indexing evidence, but no live review has occurred. | Complete phone/desktop, keyboard/screen-reader, error-state, and indexing decisions. |

Recommendation remains **DO NOT LAUNCH YET**. The PR is suitable for review and merge after hosted CI passes; public launch still requires closing or formally disabling each unresolved High capability and completing the live gates above.

## Recommended execution order

1. Complete remaining source hardening in ASR-05–09 and ASR-12–17, including backup retention, pacing, fencing, spending bounds, and independent alerting.
2. Qualify a controlled staging deployment under ASR-19, ASR-20, ASR-21, and ASR-23, starting with publication suspended and test recipients.
3. Complete editorial model quality (ASR-18), source-rights/coverage acceptance (ASR-21), and legal/privacy sign-off (ASR-22).
4. Record browser/accessibility and indexing acceptance (ASR-24).
5. Re-run checks on the exact release digest and conduct live kill-switch, backup/deletion recovery, and on-call drills. Public launch requires no unresolved High item affecting an enabled launch capability.

Do not declare readiness merely because all PRs are merged or the component suite is green.
