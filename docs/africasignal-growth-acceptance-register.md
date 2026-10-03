# AfricaSignal growth implementation acceptance register

Date: 2 October 2026
Base: dfa16550904b36a6990be6d4cd644b4fa7a7af4c
Implementation checkout: isolated codex/africasignal-growth worktree
Status: source implementation and local qualification complete; runtime and external acceptance remain open. Growth source committed as `b84c707`; CI Node runtime updated as `795975d`. No deployment, production migration, provider call, advertiser outreach, or commercial activation.

## Task disposition

| Packet | State | Evidence / remaining gate |
|---|---|---|
| F00 | Source baseline refreshed | Remote main was fetched and the isolated branch starts at the recorded source baseline. Original checkout and attached handoff files remain untouched. |
| F01 | Source accepted | docs/commercial-editorial-invariants.md; editorial state remains authoritative. |
| F02 | Source accepted | docs/commercial-surface-privacy-design.md; Explore aside is the only first-party surface; no CSP widening. |
| F03 | Source accepted with amendments | docs/commercial-domain-contract.md; the as-built amendment records exact services and schema. |
| F04 | Source delivered | Versioned default-off global/surface/AI controls, deployment deny, privacy/legal blockers, bounded commercial workload and queue admission. Context rebuild handler is registered; optional AI/report handlers are not. |
| A00 | Complete | docs/agent-reach-backend-inventory.md records the existing YouTube-only runner boundary. |
| A01-A06 | Blocked by external facts | No named second backend, dedicated account/access proof, rights terms, useful coverage evidence, or qualified egress path is available. No generic social CLI or RSS bypass was added. |
| B01-B09 | Source delivered | Sponsor/campaign/creative/booking lifecycle, direct event measurement, Explore disclosure, private reports, admin controls, category matching, and package drafts are implemented behind default-off gates. |
| B10 | Acceptance packet prepared | docs/commercial-pilot-acceptance.md. Synthetic fixtures are not real advertisers, agreements, or prices. |
| C01-C05 | Source delivered | Versioned content-only contexts, deterministic rules, immediate invalidation, bounded scheduled rebuilds, curated match reasons, and revision-bound package proposals. |
| C06-C09 | Deferred optional scope | The plan marks these optional after a useful deterministic pilot. No commercial provider transfer has been approved or selected; deterministic outcomes remain sufficient and AI output cannot be safely enabled without a separate review gate and acceptance work. |
| P01-P07 | Blocked by external facts | No programmatic provider/account, eligible account evidence, reviewed slot IDs, consent decision, or browser-origin contract has been selected. No browser adapter or CSP exception was invented. |
| Q01 | Local qualification complete | Disposable PostGIS upgraded from 0039 through 0046; source seed created 16 permissions; restore drill restored schema 0046 and compared 50 tables. After freezing the public test clock, the full offline suite passed 2,127 tests with 41 skipped in 2,014.64 seconds. Booking concurrency and measurement replay are covered. |
| Q02 | Pending runtime review | Automated route/CSRF coverage does not establish desktop/mobile, keyboard, screen-reader, or visual browser acceptance. No browser claim is made. |
| Q03 | Local checks complete | `ruff check .`, `ruff format --check .`, and `mypy src` pass (190 source files); `pip check` passes. `pip-audit` found no known vulnerabilities, but skipped the local `africasignal==0.1.0` package because it is not published on PyPI. App and backup Docker images both build. The final offline `pytest -q` run passed 2,127 tests with 41 skipped in 2,014.64 seconds (`RUN_NETWORK_TESTS=0`). |
| Q04 | Prepared | See docs/africasignal-growth-staging-plan.md. It is a plan only. |
| Q05 | Pending named external owners | Rights, privacy/counsel, advertiser terms, staging operations, browser/accessibility, and any future provider/AI transfer reviewers are not named in repository evidence. |
| Q06 | Not source/runtime qualified for release | Only B and deterministic C source work is delivered. External A/P gates and runtime reviews remain open; release is not recommended. |

## Delivered source boundaries

- New commercial tables are additive: controls, sponsor/category, campaign/creative, content context, exclusive topic booking, delivery events/aggregates, and revision-bound package drafts.
- Explore renders one disclosed first-party sponsor aside only when live eligibility checks pass. Public pages remain no-store; invalid redirects fail closed.
- Context projections are bound to content hashes, evidence IDs, source IDs, taxonomy/classifier versions, and expiry. Publication corrections/withdrawals, source permission/activity changes, evidence retention expiry, and publication-suspension changes invalidate in the same transaction.
- Rebuild jobs are deduplicated, cursor-rotated, limited to the configured maximum 10 items per job, admitted by deployment/global/workload controls and schedule, and fenced by queue leases. Jobs recheck current version/hash and lock the current situation/evidence/sources before storing.
- Matching uses a fixed category-to-topic mapping and reason codes. There is no fit score, reader inference, or public match API.
- Package drafts snapshot current eligible content, existing overlapping booking revisions, campaign/sponsor/control revisions, and recorded integer NGN kobo fee state. Approval is revision-bound and does not create a booking.
- Commercial test data uses reserved example domains and synthetic records. It does not represent a real advertiser, agreement, agreed commercial price, or provider account.

## External acceptance register

| Gate | State | Accountable owner/evidence |
|---|---|---|
| Second acquisition backend | Pending | Product owner must name a candidate and supply dedicated access, current rights/terms, quota and useful energy/food coverage evidence. |
| Source-rights/legal review | Pending | A designated rights/legal reviewer and source-specific approval evidence are not recorded. |
| Direct-sponsorship privacy and lawful-basis review | Pending | A named privacy/counsel reviewer must accept the revised notice, event purpose, retention and applicable consent conclusion. The source setting is only a gate, not legal proof. |
| Real advertiser agreement and fee | Pending | A named commercial owner, real advertiser, signed/manual terms and integer NGN fee must be entered through the admin process. No outreach was performed. |
| Staging host/secrets/egress/monitoring | Pending | An operations owner and authorized isolated host, secret store, egress policy, test cohort, monitoring, backup and rollback evidence are not available. |
| Browser/accessibility acceptance | Pending | A named UX/accessibility reviewer must record desktop/mobile, keyboard, screen-reader, expiry/pause, empty-state, and redirect results in an actual browser. |
| Programmatic provider/account/consent | Not selected | Product/legal must select a provider and demonstrate account eligibility, consent/geography, data map, allowed origins and reconciliation basis before P01. |
| Commercial AI transfer and route review | Pending/deferred | No route/purpose is added. A named privacy owner must approve the content transfer and a reviewed route before C06-C09. |

No owner name or evidence is inferred from a test fixture or a switch value.

## Result fields to complete

- Result commits: `b84c707` (growth implementation) and `795975d` (Node 24 CI actions), pushed to `codex/africasignal-growth`. Further runtime-hardening changes are documented separately.
- Exact local checks: Ruff lint and format pass; mypy succeeds on 190 source files; pip check passes; pip-audit reports no known vulnerabilities and skips the unpublished local package. Final offline full suite passes 2,127 tests with 41 skipped in 2,014.64 seconds (`RUN_NETWORK_TESTS=0`). A first run exposed a collection-time token expiry in the public test; freezing the route test clock fixed it, and the affected module passes 6/6. The GDELT raw-byte fixture group passes 7/7 with explicit LF checkout rules.
- Database/image checks: disposable PostGIS upgrade 0039 -> 0046 and source seed pass; local backup/restore drill passes with 50 tables compared; app and backup images both build.
- Runtime acceptance: pending; not inferred from TestClient or source tests.
- Rollback: keep COMMERCIAL_DENY true, switch global/surface off, stop commercial admission, suppress/invalidate public projections, and retain audit/uncertain event evidence. Prefer forward-compatible source fixes; no live downgrade is authorized.
