# AfricaSignal production acceptance record

No acceptance is implied by this template. Complete it against one exact release commit and the exact app and backup image digests; attach evidence by durable link or repository artifact reference. A blank, simulated, or mock-only entry remains open.

## Release identity

- Commit SHA:
- App image digest:
- Backup image digest:
- SBOM and image scan references:
- Date and environment:
- Accountable release owner:

## Engineering and operational gates

| Gate | Result: pass / fail / blocked | Evidence reference | Reviewer and date |
|---|---|---|---|
| ASR-05 source rights and retained-text policy across all storage tiers | | | |
| ASR-06 evidence and backup expiry, shared-object handling, deletion verification | | | |
| ASR-07 deletion-ledger restore, bucket-loss recovery and quarantine replay | | | |
| ASR-08 key generation, rotation, escrow and recovery drill | | | |
| ASR-09 robots policy, crawl pacing, redirect and timeout behavior | | | |
| ASR-10 concurrent assess/correct/withdraw on production-like PostgreSQL | | | |
| ASR-12 worker attempt fencing and irreversible-effect recovery | | | |
| ASR-13 spend cap, configured prices, provider-side limits and invoice reconciliation | | | |
| ASR-14 proxy topology, shared/API limits and abuse controls | | | |
| ASR-15 stale/withdrawn output on every enabled channel | | | |
| ASR-16 web readiness, worker/scheduler/backup monitors, notification route and on-call drill | | | |
| ASR-17 measured load, table growth, archival and recovery targets | | | |
| ASR-19 controlled staging deployment, restore, rollback, RPO and RTO | | | |
| ASR-20 sender-domain/provider acceptance, suppression and test-recipient controls | | | |
| ASR-23 exact-digest CI, provenance, image scan and enforced repository rules | | | |

## Human acceptance gates

| Gate | Evidence and named accountable approver | Date | Result |
|---|---|---|---|
| ASR-18 Editorially labelled real/recorded model evaluation; number accuracy 100%, T1 evidence-state >=90%, T2 >=80% | | | |
| ASR-21 Rights-approved source register and verified topic/location coverage | | | |
| ASR-22 Qualified counsel privacy/legal decision record and named operator | | | |
| ASR-24 Phone/desktop, keyboard, screen-reader, error-state and indexing decision | | | |

## Exact-release drills

- CI/test result tied to the image digest:
- Publication kill-switch suspend and resume test; final state:
- Backup checksum and object verification:
- Restore duration and measured RPO/RTO:
- Account deletion replay and expired-evidence redaction:
- Failed migration and prior-digest rollback:
- Worker, scheduler, storage and mail failure monitor timestamps:
- Final open High findings and disabled capabilities:
- Launch decision and accountable approver:
