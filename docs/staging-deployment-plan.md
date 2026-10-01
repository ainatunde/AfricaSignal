# Repeatable staging deployment plan

Status: plan prepared; no staging host or cloud provider has been authorized or supplied. This document is not evidence of a staging deployment or acceptance.

The staging Compose file is docker-compose.staging.yml. It deliberately uses externally provisioned PostgreSQL/PostGIS and S3-compatible storage. It creates no database, bucket, public DNS, TLS endpoint, or provider account. Supply immutable app and backup image references by digest; never use a mutable tag for acceptance.

## Safety properties

- The migration service runs staging initialization before the web, worker, or scheduler services start. That idempotently sets publication_suspended to true on every deployment.
- Staging email dispatch is blocked unless the normalized recipient address appears in STAGING_EMAIL_ALLOWED_RECIPIENTS. An unlisted recipient is marked dead without a provider send and its sign-in link is scrubbed.
- The web port binds to loopback on port 8080. Put an HTTPS reverse proxy in front of it with staging access restrictions; do not publish port 8000 directly.
- Worker and scheduler heartbeats are optional in source but required for staging acceptance. Configure distinct external monitor URLs and verify each monitor independently.
- Database dumps are encrypted outside development. The backup bucket must be separate from the application bucket. Evidence and deletion-ledger prefixes are mirrored to current source state with checksum-based sync and content-download verification; other object prefixes are copied immutably.

## Required staging inputs

Provision unique staging resources and credentials, not production copies:

- A restricted staging hostname and HTTPS reverse proxy with access limited to the review team.
- A staging PostgreSQL/PostGIS database, an app runtime role, and a migration-owner role. MIGRATION_DATABASE_URL is used only by the one-shot migration service; DATABASE_URL is used by app services.
- A private staging evidence bucket and a private backup bucket, preferably in separate accounts or providers. Give the app role no backup-bucket access and the backup job no unrelated cloud permissions.
- A fresh SECRET_KEY and a separately escrowed backup passphrase. Keep them in the host secret manager and inject them when Compose starts; do not put values in Git, a command transcript, or a shared terminal.
- STAGING_EMAIL_ALLOWED_RECIPIENTS containing only controlled test addresses, plus a provider sandbox or staging-only sender domain.
- A low staging LLM daily budget. Leave ANTHROPIC_API_KEY unset until model evaluation is deliberately scheduled; any key used must be a staging key with provider-side spend controls.
- Independent WORKER_HEARTBEAT_URL, SCHEDULER_HEARTBEAT_URL, and BACKUP_HEARTBEAT_URL checks routed to the on-call monitor.

Use an isolated source registry with only sources whose staging collection is permitted. Do not import production reader accounts, real email lists, or unrestricted production secrets. Keep one worker replica until a shared cross-replica source pacer is implemented and qualified; the current acquisition limiter is process-local. Fetch deadlines bound HTTP requests and robots-policy reads, but synchronous operating-system DNS resolution cannot be interrupted and may exceed that deadline.

## Deployment sequence

1. Record the commit SHA and build the app and backup images from that checkout. Scan both images, retain their SBOMs, and record the registry-returned sha256 digests. Set AFRICASIGNAL_IMAGE and AFRICASIGNAL_BACKUP_IMAGE to those exact digest references.
2. Put staging inputs in the deployment host's protected secret mechanism. Check the resolved Compose configuration without printing or saving secret values in a ticket or build log. Confirm the database and app bucket names are staging-specific and that the backup bucket differs from the app bucket.
3. Run the migration service and wait for a successful exit. It applies migrations, seeds sources without approving them, and forces publication suspension on. Verify the setting in the staging database before starting the other services.
4. Start web, worker, and scheduler. Confirm the web health check, login to the restricted staging console, check that source approvals remain explicit, and verify all three heartbeats from the independent monitor. A failed DB/scheduler cycle must fail the scheduler heartbeat.
5. Keep publication suspended. Exercise sign-in, account deletion/export, correction review, freshness and withdrawal handling, and email delivery only to allowlisted test recipients. Confirm an unlisted recipient is never sent.
6. Run a staging backup, inspect dump checksum and encryption metadata, and run a latest-backup restore drill into a separate scratch database and empty restore bucket. Reapply account deletions and current retention/redaction while the restored app is quarantined. Verify evidence references, deletion ledger, web, jobs, and measured RPO/RTO before clearing quarantine.
7. Exercise the kill switch in the restricted staging environment, then leave it suspended. Test failed migration/rollback and the documented prior-digest rollback without pointing the candidate at production storage.
8. Save dated browser and keyboard/screen-reader results, real model evaluation results, source-rights/coverage decisions, counsel/privacy sign-off, exact image digests, test logs, and recovery/on-call evidence in the acceptance record.

Do not resume publication in staging as a substitute for editorial, source-rights, legal, or production approval. The staging check is complete only when an accountable reviewer has signed each applicable section in docs/production-acceptance-record.md. Do not call this production-ready while an unresolved High finding affects an enabled launch capability.

## Acceptance record and stop conditions

Record the actual staging host/provider, database and bucket identifiers (never credentials), image digests, migration result, mail provider sandbox, monitor checks, backup checksum/restore duration, deletion replay result, kill-switch result, rollback evidence, reviewers, and date. Mark a gate blocked when the required external system or accountable approver is unavailable; do not substitute a mock or a source-level test for runtime acceptance.
