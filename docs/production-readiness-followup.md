# Production-readiness remediation follow-up

Date: 2026-10-01
Branch: codex/production-readiness-followup
Candidate source commit: d35424685c84f2481e79f04934382e90d35279ae
Status: source remediation and local Linux/runtime verification passed. Staging plan is prepared only. This is not production acceptance or launch approval.

## Implemented in source

- Job claims carry fresh UUID fencing tokens. Heartbeat, completion, and failure require the matching owner and token plus an unexpired lease; migration fences claims that were already running.
- Model calls reserve an upper-bound input/output cost before sending, enforce per-job token limits, and keep ambiguous network outcomes as uncertain reservations. Spend views include reserved amounts.
- Evidence and deletion-ledger backup prefixes mirror current state with checksum sync and downloaded content verification. Other object prefixes remain immutable. Restores stay quarantined until deletion replay and retention cleanup complete.
- Fetching applies robots Crawl-delay and Request-rate and carries the whole-fetch deadline into robots reads, pacing waits, and HTTP requests. Synchronous operating-system DNS cannot be interrupted and may overrun the deadline. The token bucket remains process-local, so multiple worker replicas are not qualified.
- Worker and scheduler heartbeats can report start, failure, and recovery to independent HTTPS monitors. Staging email uses a normalized test-recipient allowlist; staging initialization reasserts publication suspension before app services start.
- Production containers upgrade available base OS packages at build time. Backup and CI use rclone v1.75.1 from the official static archive with a pinned SHA-256. Object restore copies use checksum comparison.
- A provider-neutral staging Compose file, deployment sequence, acceptance record, and runbook are prepared. Schema/interpolation was checked with inert placeholders; no service was started.

## Final local verification

- Linux suite from the source tree matching candidate commit d35424685c84f2481e79f04934382e90d35279ae, on a disposable PostgreSQL 16/PostGIS 3.4 database: 2,071 passed, 5 skipped in 805.57 seconds.
- ruff check: passed. ruff format --check: passed (490 files). mypy: passed (160 source files). pip-audit -r requirements.lock: no known vulnerabilities.
- Staging Compose schema and interpolation: passed with 28 inert placeholder variables. No deployment, external host, bucket, or provider credentials were used.
- The app runtime image passed a local container smoke check against the disposable database: /healthz returned HTTP 200 and {"ok":true,"db":true}. The database-backed publication switch passed suspend, resume, and final-suspended checks; this was an isolated local drill.
- The repository restore-drill.sh created a local database backup, verified its SHA-256 (04b6cffbd212432d3d941a4a8e770587c04b6813c66bd70f365cc7e50392b3dc), restored schema 0035 in 3 seconds, and compared 36 tables. The scratch database was dropped. No object bucket, evidence object, deletion replay, key recovery, or representative RPO/RTO was exercised.
- Local Linux/amd64 OCI artifacts were built from candidate commit d35424685c84f2481e79f04934382e90d35279ae with BuildKit SBOM/provenance attestations. These are local build digests, not registry-returned references; neither image was pushed.
  - App index: sha256:4bdfbb70eb18e3fd5d9613ebd1fc3fe85365b8a38cc9c599208f519ac64148ae; runtime manifest: sha256:94ad6df40efa4906ad2a2faf36ed61214d2a3bd37b1b9005d646df3b183a6ede; config: sha256:9d64157bbff011cebf6c1a5d7edcb92523549c8ade9589bdedffaa9114bacc83; attestation manifest: sha256:05c85bfc5c028e0234b49ed30164e70ec064e358b15d3f0d4934fc4b3dd3e61f.
  - Backup index: sha256:c042e75f4e4f80ca3f6c64c6c0d14fa049e00a2af2cc40ee1aaa9edc2cac6cee; runtime manifest: sha256:16140d58a922cc83e47dc8b1dd7ae9734ba9f8a5c890cc2ec2c0091621633862; config: sha256:eaf09dd97b19a3f228cd3550680478e57c1a57522caccf5d043f3e9992d3e6c; attestation manifest: sha256:4c5a6bac5e750e402f47397f34bd6592eba52847923ea84d73714a11e60b1773.

## Image scan interpretation

Trivy 0.75.0 scanned the runtime image contents. The generic NVD severity feed reported app OS findings at 1 Critical and 76 High, plus Python findings at 4 High and 2 Medium; backup OS findings at 55 High, Python at 4 High and 2 Medium, and the rclone Go binary at 1 High and 1 Unknown. Generic version matching does not account for every Debian backport or package disposition, so these counts are not a launch decision by themselves.

With Debian severity data, neither image had a Critical or High severity assignment. The app scan contained 114 Low and 195 Unknown OS findings, plus six Unknown Python entries. The backup scan contained 87 Low and 166 Unknown OS findings, six Unknown Python entries, and two Unknown rclone entries. Debian's feed still marks the installed app libxml2 package as affected by CVE-2026-86138 and related advisories, with no stable Trixie fixed version listed. CVE-2026-6653 is recorded by Debian as a minor no-DSA issue. The generic feed rates these advisories more severely. The app image includes libxml2 alongside the OCR stack, so this remains an unresolved security gate pending a fixed supported base, reachability review, or disabling the affected capability. See the [Debian libxml2 tracker](https://security-tracker.debian.org/tracker/source-package/libxml2), [CVE-2026-86138](https://security-tracker.debian.org/tracker/CVE-2026-86138), and [CVE-2026-6653](https://security-tracker.debian.org/tracker/CVE-2026-6653).

Trivy's Python High entries came from third-party SBOM data that named msgpack 1.1.2, setuptools 70.3.0, and urllib3 2.7.0. Direct inspection of the app runtime image found no msgpack, setuptools 84.0.0, and urllib3 2.8.0; the locked Python dependency audit also passed. Trivy documents that third-party SBOM paths and relationships may be interpreted inaccurately: [container image SBOM scanning](https://trivy.dev/docs/latest/target/container_image/). The backup Go finding is CVE-2026-84445 in grpc-go and needs call-path applicability review before acceptance; Debian's feed reports it as fixed with Unknown severity.

## Acceptance gates still open

- ASR-05-08: source rights and retained-text policy, all storage-tier expiry, shared-object handling, independent deletion-ledger durability, bucket-loss recovery, key rotation, escrow, and recovery need owner approval and real storage drills.
- ASR-09 and ASR-14: publisher-specific behavior, shared cross-replica pacing, API/edge limits, proxy topology, and abuse controls remain unqualified. Synchronous DNS may exceed the fetch deadline.
- ASR-10 and ASR-12: production-like concurrent corrections/withdrawals and crash/lease-loss recovery, including intermediate and irreversible effects, remain untested outside the local suite.
- ASR-13: configured prices and token bounds are estimates; provider-side limits and invoice reconciliation need evidence.
- ASR-15-17: enabled-channel freshness/withdrawal, operational-table archival, representative load/growth, disk, queue-drain, and recovery targets need runtime evidence.
- ASR-16, ASR-19, ASR-20, and ASR-23: the user scoped this work to a repeatable staging plan only. Actual staging deployment, independent monitor delivery, provider email/suppression behavior, registry-pushed exact digests and attestations, repository rules, rollback, recovery, notification, and on-call drills remain blocked. Local image digests are not staging registry digests.
- ASR-18, ASR-21, ASR-22, and ASR-24: editorially labelled model evaluation, source-owner rights/coverage approval, qualified legal/privacy sign-off, and dated browser/accessibility/indexing acceptance require named human reviewers. No approval is inferred.

Recommendation: DO NOT LAUNCH while the app image carries unresolved affected libxml2 advisories and the required staging, editorial, rights, legal/privacy, accessibility, and operational acceptance evidence is absent. Launch only after every unresolved High item affecting an enabled capability is fixed, independently reviewed, or the capability is disabled, and all applicable exact-release gates are signed.
