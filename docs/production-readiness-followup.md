# Production-readiness remediation follow-up

Date: 2026-10-01  
Branch: codex/production-readiness-followup  
Status: remediation changes and local Linux verification are complete for this branch. This is not a production launch approval.

## Implemented and verified in source

- Job claims now carry a fresh UUID fencing token. Heartbeat, completion, and failure updates require the same claim owner and token and an unexpired lease; the migration fences claims already running during rollout.
- Model calls reserve an upper-bound input/output cost before sending, account for per-job tokens, and retain ambiguous network outcomes as uncertain reservations. Dashboard alerts and spend views include reserved amounts.
- Evidence and deletion-ledger backup prefixes mirror current state. Sync compares checksums; a downloaded content check verifies the mirror. Other object prefixes remain immutable copies. Restore must remain quarantined until current deletion replay and retention cleanup complete because older database dumps can reference evidence that has since expired.
- Fetching applies robots Crawl-delay and Request-rate, and passes the remaining whole-fetch deadline into robots reads, pacing waits, and HTTP requests. Operating-system DNS resolution is synchronous and cannot be interrupted; it can still overrun the deadline. The token bucket remains process-local, so do not scale workers across replicas until shared pacing is implemented and qualified.
- Worker and scheduler heartbeats can report start, failure, and recovery to independent HTTPS monitors. Staging email sends are restricted to a normalized test-recipient allowlist; staging initialization reasserts the publication hold before application services start.
- A provider-neutral staging Compose file, deployment sequence, acceptance record, and runbook updates are prepared. Compose schema/interpolation validation used inert placeholders and started no services.

## Verification

- Complete Linux suite, using africasignal:linux-audit and the disposable PostgreSQL 16 database africasignal_test: **2,071 passed, 5 skipped** (703.13 seconds).
- ruff check .: passed.
- ruff format --check .: passed (330 files).
- mypy src: passed (160 source files).
- pip-audit -r requirements.lock: no known vulnerabilities.
- docker compose -f docker-compose.staging.yml config --quiet: passed with inert placeholder values; no staging service was created or started.
- The separate pip-audit --local scan reported known advisories in pip 24.0 inside the Windows audit virtual environment. That tool environment is not the application dependency lock; both production Dockerfiles and CI pin pip 26.2.1.

## Gates still open

- **ASR-05–08:** confirm source rights and text retention across real storage tiers, shared-object expiry, independent deletion-ledger durability, bucket-loss recovery, key rotation and escrow.
- **ASR-09 / ASR-14:** exercise against real publishers and the chosen topology; shared cross-replica acquisition pacing and API/edge limits are not implemented or qualified. Synchronous DNS is outside the enforceable fetch deadline.
- **ASR-10 / ASR-12:** production-like concurrent assessment and worker crash/lease-loss drills are not recorded. The claim token fences terminal job updates, but every intermediate and irreversible effect still needs acceptance.
- **ASR-13:** prices and token bounds are configured estimates; reconcile provider bills and approve the spend cap.
- **ASR-15–17:** accept freshness across enabled channels, define safe operational-table archival, and record representative load, growth, disk, queue-drain, and recovery evidence.
- **ASR-16 / ASR-19 / ASR-20 / ASR-23:** provision actual staging systems and external monitors; verify real email-provider behavior; build/scan exact release images and retain registry SBOM/provenance; enforce repository rules; then record rollback, recovery, notification, and on-call drills. This task was scoped to a repeatable staging plan, so no live deployment was attempted.
- **ASR-18 / ASR-21 / ASR-22 / ASR-24:** require editorially labelled model cases and sign-off, source-owner rights and coverage approval, qualified legal/privacy sign-off, and dated browser/accessibility/indexing acceptance. No human or vendor approval is inferred here.

Recommendation remains **DO NOT LAUNCH** until each unresolved High item is closed or its capability is disabled, and all applicable exact-release acceptance records are complete.
