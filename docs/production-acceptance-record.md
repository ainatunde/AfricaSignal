# AfricaSignal production acceptance record

This record contains local evidence only. No acceptance or launch approval is implied. Gates marked blocked need an external system or named accountable reviewer before any enabled capability can be accepted.

## Release identity

- Candidate source commit used for the runtime images: d35424685c84f2481e79f04934382e90d35279ae
- App local OCI index: sha256:4bdfbb70eb18e3fd5d9613ebd1fc3fe85365b8a38cc9c599208f519ac64148ae
- App runtime manifest/config: sha256:94ad6df40efa4906ad2a2faf36ed61214d2a3bd37b1b9005d646df3b183a6ede / sha256:9d64157bbff011cebf6c1a5d7edcb92523549c8ade9589bdedffaa9114bacc83
- App BuildKit SBOM/provenance attestation manifest: sha256:05c85bfc5c028e0234b49ed30164e70ec064e358b15d3f0d4934fc4b3dd3e61f
- Backup local OCI index: sha256:c042e75f4e4f80ca3f6c64c6c0d14fa049e00a2af2cc40ee1aaa9edc2cac6cee
- Backup runtime manifest/config: sha256:16140d58a922cc83e47dc8b1dd7ae9734ba9f8a5c890cc2ec2c0091621633862 / sha256:eaf09dd97b19a3f228cd3550680478e57c1a57522caccf5d043f3e9992d3e6c
- Backup BuildKit SBOM/provenance attestation manifest: sha256:4c5a6bac5e750e402f47397f34bd6592eba52847923ea84d73714a11e60b1773
- Date/environment: 2026-10-01; local Docker Desktop Linux/amd64, disposable PostgreSQL 16/PostGIS 3.4. No staging deployment. OCI artifacts are local and were not pushed to a registry.
- Accountable release owner: unassigned. Reviewer/sign-off: none.
- Scan evidence: local Trivy 0.75.0 image archives, compared under generic NVD and Debian severity data; details and SBOM caveats are in docs/production-readiness-followup.md. The generic scan has open findings; Debian data marks the installed libxml2 advisories affected with Unknown severity.

## Engineering and operational gates

| Gate | Result | Evidence and remaining work | Reviewer and date |
|---|---|---|---|
| ASR-05 source rights and retained-text policy across all storage tiers | blocked | Source-level controls exist; no source-owner rights or retention approval across real storage tiers. | None |
| ASR-06 evidence and backup expiry, shared-object handling, deletion verification | blocked | Checksum mirroring is implemented. No real-bucket shared-object expiry/deletion drill or independent verification. | None |
| ASR-07 deletion-ledger restore, bucket-loss recovery and quarantine replay | blocked | Local DB backup restore passed in 3 seconds, schema 0035, 36 tables. No object-bucket loss, deletion replay, redaction or quarantine acceptance. | None |
| ASR-08 key generation, rotation, escrow and recovery drill | blocked | No staging secret manager, escrow owner, or key recovery drill. | None |
| ASR-09 robots policy, crawl pacing, redirect and timeout behavior | blocked | Source changes and Linux suite passed. No real publisher acceptance or shared cross-replica pacing; synchronous DNS can exceed deadline. | None |
| ASR-10 concurrent assess/correct/withdraw on production-like PostgreSQL | blocked | Local PostgreSQL integration suite passed; no production-like concurrency/load or channel withdrawal acceptance. | None |
| ASR-12 worker attempt fencing and irreversible-effect recovery | blocked | Claim fencing is implemented and tests passed. No live worker crash/lease-loss drill for every intermediate and irreversible effect. | None |
| ASR-13 spend cap, configured prices, provider-side limits and invoice reconciliation | blocked | Per-job budget reservation is implemented. Prices are estimates; no provider-side limits or bill reconciliation. | None |
| ASR-14 proxy topology, shared/API limits and abuse controls | blocked | Current limiter is process-local. No approved proxy topology, shared/API controls, or abuse drill. | None |
| ASR-15 stale/withdrawn output on every enabled channel | blocked | No live channel or stale/withdrawn-output acceptance. | None |
| ASR-16 web readiness, worker/scheduler/backup monitors, notification route and on-call drill | blocked | Local app-image /healthz returned 200 with database available. No external monitors, notification delivery, or on-call drill. | None |
| ASR-17 measured load, table growth, archival and recovery targets | blocked | No representative load, growth, archival, queue-drain, RPO or RTO evidence. | None |
| ASR-19 controlled staging deployment, restore, rollback, RPO and RTO | blocked | User scope was a repeatable deployment plan only. No staging system was provisioned or started. | None |
| ASR-20 sender-domain/provider acceptance, suppression and test-recipient controls | blocked | Source restricts staging email recipients. No sender domain, provider sandbox, suppression or delivery evidence. | None |
| ASR-23 exact-digest CI, provenance, image scan and enforced repository rules | blocked | Local app/backup OCI indexes, runtime configs, SBOM/provenance attestations and scans are recorded above. They were not pushed; registry digests, GitHub CI results, repository protection and release-signing rules are unverified. App libxml2 advisories remain affected. | None |

## Human acceptance gates

| Gate | Result | Evidence and remaining work | Accountable approver and date |
|---|---|---|---|
| ASR-18 Editorially labelled real/recorded model evaluation; number accuracy 100%, T1 evidence-state >=90%, T2 >=80% | blocked | No labelled evaluation set, reviewer results, or editorial sign-off. | None |
| ASR-21 Rights-approved source register and verified topic/location coverage | blocked | No source-owner authorization or coverage approval. | None |
| ASR-22 Qualified counsel privacy/legal decision record and named operator | blocked | No qualified counsel decision, privacy sign-off, or named operator. | None |
| ASR-24 Phone/desktop, keyboard, screen-reader, error-state and indexing decision | blocked | No dated browser/accessibility/indexing review or accountable reviewer. | None |

## Exact-release drills

- Test result: 2,071 passed, 5 skipped on Linux against disposable PostgreSQL 16/PostGIS 3.4, from a source tree matching the candidate code commit. The tests were not run inside the production image or by GitHub CI.
- App runtime smoke: exact local runtime image returned HTTP 200 from /healthz with database true. No external ingress or staging host.
- Publication kill switch: local app-image drill passed suspend, resume, and final suspended in the disposable database. No channel/provider side effects were exercised.
- Backup integrity: local dump SHA-256 04b6cffbd212432d3d941a4a8e770587c04b6813c66bd70f365cc7e50392b3dc; local restore passed in 3 seconds, 36 tables compared, schema 0035. No S3-compatible object data was included.
- Account deletion replay and expired-evidence redaction: not run against real evidence storage; blocked.
- Failed migration and prior-digest rollback: not run; blocked.
- Worker, scheduler, storage and mail failure monitor timestamps: no independent monitor/provider configured; blocked.
- Remaining security issue: app image libxml2 is reported affected by CVE-2026-86138 and related advisories in Debian's tracker; generic scanner rates libxml2 advisories High/Critical. Rclone grpc advisory requires call-path review. See the follow-up report.
- Launch decision: DO NOT LAUNCH. Accountable approver: none. This record is not an approval.
