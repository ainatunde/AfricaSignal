# Runtime hardening — 2026-10-03

Baseline: `795975d` on `codex/africasignal-growth` (growth source `b84c707`, current main `dfa1655`). This is source remediation and local qualification, not a production activation.

## Changes

- CI uses Ubuntu 24.04, immutable Node 24 action revisions, read-only token permissions, and no persisted checkout credential. Pushes to the growth branch, PRs, merge groups and manual dispatch run the existing `test` job. PostgreSQL 16 clients, OCR, rclone and Moto server imports are explicit prerequisites. `REQUIRE_OPS_TESTS=1` fails the run if any operations test skips.

- Development dependencies include Moto's server extra. The S3 checks also exposed repeated backup console initialization inside shell substitutions; initialization now runs once in the parent shell. Locks retain reviewed runtime pins and explicit Linux/Windows platform dependencies. Use pip-tools 7.6.1. Regenerate the development lock on Linux with `python -m piptools compile --extra=dev --constraint=requirements.lock --output-file=requirements-dev.lock --strip-extras pyproject.toml`, then run `python scripts/normalize-lock-platforms.py`. Review pin changes and run installation/audit checks before committing.

- Worker attempts carry cancellation state. Handler flushes and intermediate commits check ownership; commits lock the job row until the transaction finishes. Effect dispatch locks the row against reclaim and preserves existing provider identities. Nested storage fences share the enclosing fence. Anthropic SDK retries are disabled so one reservation dispatches at most one HTTP call; the durable queue owns retry/reconciliation. Terminal cleanup serializes against manual retries. Fetch/backfill loops check cancellation. This does not create exactly-once provider delivery: a process crash after provider acceptance still needs provider idempotency and reconciliation.

- DNS runs in isolated, killable child processes with a maximum of eight concurrent lookups and a five-second cap, further bounded by the fetch/preflight deadline. Timeout kills/reaps the child and releases capacity. All returned addresses must remain public; TCP pinning and original hostname TLS verification remain in place. Children intentionally do not inherit monkeypatched resolvers; offline tests mock the explicit DNS boundary.

- Staging/production API limits use atomic PostgreSQL sliding windows, with distinct scopes for each route. Only secret-keyed HMAC client keys and bounded timestamps are stored. Crawler budgets use shared token buckets and the strictest active domain policy; changing configuration does not refill a bucket. Development retains local limiters. Backend failure refuses work, and the dedicated limiter pool has bounded connection, lock and statement waits.

- Migration `0047` adds shared limit state, permanent job deduplication keys, daily archival totals, and the completed-job retention index. It backfills existing dedupe keys and retains monetary call records when completed job rows are removed.

- The scheduler runs bounded operational cleanup every minute (maximum 1,000 completed jobs and 1,000 expired limit states per run). Completed job payloads remain for 90 days. Dead jobs remain available for diagnosis/manual retry; jobs with unresolved spend reservations remain until reconciliation. Financial records and audit history are retained; no legal/financial retention period is invented. Dedupe tombstones are retained indefinitely to prevent replay. Overdue cleanup raises `retention_backlog` in existing console alerts.

## Rollout and limitations

Apply migration `0047` before starting the updated application or workers. Quiesce the old application, scheduler and workers for the upgrade: old workers cannot participate in the new effect fences, and old queue writers do not honor archived dedupe keys. Rollback cannot restore already archived payloads; use a tested backup/recovery procedure if those payloads are needed. Downgrading removes the compact dedupe registry, so do not downgrade after archival without preserving/replaying tombstones.

Shared limits require the same application secret across replicas, as existing encrypted settings already do. An intentional secret rotation changes limiter keys and needs an edge policy during rotation. Validate the real proxy topology, provider timeouts and publisher-specific rates before launch. The stricter active crawler policy relaxes only after its idle state expires.

The compact dedupe registry, financial/audit history and unresolved dead work still consume space. Monitor backlog and database growth; these changes do not establish a production capacity claim or authorize financial/audit record deletion.

## Qualification

- Ruff check and formatting pass across 421 files; strict mypy passes across 196 source files. `pip check` passes. Linux `pip-audit` reports no known vulnerabilities; the unpublished local package is not on PyPI and is skipped.

- Windows network/cache subset: 126 passed. Linux PostgreSQL runtime, queue, migration, provider and S3 checks passed in the focused run; the final runtime-hardening module, including the migration-backfill cleanup regression, passed 11 tests in 7.34 seconds.

- Full offline suite before the last test-isolation correction: 2,179 passed, 3 skipped, 1 failed in 1,565.66 seconds. The sole failure was the new unresolved-spend test seeing the old job inserted by its preceding migration-backfill test. That fixture now deletes only its own job and dedupe key; the complete 11-test module passes afterward. The three skips were a real LLM recording (no API call was made), its empty parameter set, and the full boundary dataset (the local run omitted `RUN_NETWORK_TESTS=1`). CI enables the boundary download. The first runner attempt's shell-script launch errors came from my source tar omitting Git executable bits; I corrected the snapshot to the tracked `100755` modes before this full run.

- A production-path backup/restore drill on disposable PostgreSQL passed after upgrading through `0047`: checksum verified, restored schema `0047`, and compared all 53 tables. The application and backup images built; their operating-system package layers were cached, so these were not no-cache builds. The final app image passed non-root/content checks; the backup image is being rebuilt with the final shell initialization change.

Real provider crash reconciliation, deployed proxy/edge and storage acceptance, image-security disposition, legal/source-rights approvals, browser acceptance and representative production load remain release gates. GitHub required-check enforcement is a repository setting and is not established by editing this workflow.
