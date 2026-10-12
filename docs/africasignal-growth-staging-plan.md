# AfricaSignal growth staging plan

Date: 2 October 2026
State: prepared instructions only. No staging host was selected and no migration or deployment was run.

## Intended scope

This plan covers only the first-party direct sponsorship and deterministic content-context source implementation delivered in the isolated worktree. Acquisition beyond the existing YouTube metadata bridge and programmatic delivery remain out of scope until their external prerequisites are evidenced. Optional AI classification, report prose, and creative suggestions remain deferred.

## Preconditions

A staging run must not begin until an accountable owner records all of the following:

1. A dedicated staging host and DNS/TLS endpoint, with admin MFA, named operators, independent monitoring, and tested rollback access.
2. An isolated PostgreSQL/PostGIS database and object store, plus a restore drill with recorded recovery time and data comparison. No production credentials or storage are reused.
3. Secret-manager entries for only the services actually enabled. Secrets are never pasted into prompts, logs, job payloads, or this plan.
4. Egress rules for the exact enabled services. Direct first-party sponsorship needs no third-party browser/ad request; a later acquisition or programmatic adapter requires its own fixed destinations and network tests.
5. A designated rights/legal reviewer, a privacy/counsel reviewer, an editorial owner, an operations owner, and a browser/accessibility reviewer.
6. A documented measurement purpose and acceptance of the revised privacy notice and event retention. The database gate commercial_privacy_review_confirmed is an operator setting, not proof of reviewer acceptance.
7. Any real advertiser, written manual terms, and integer NGN fee entered by an authorized commercial owner. Synthetic example.org tests are not advertiser qualification or pricing evidence.

Until all enabled-scope gates pass, keep deployment COMMERCIAL_DENY true, all commercial switches false, the commercial workload off, and publication suspension behavior unchanged.

## Direct sponsorship qualification sequence

1. Record the exact source commit and image digest. Review migrations and use a backup-tested, isolated staging database. Do not use a live downgrade as rollback.
2. Keep the deployment deny set. Apply schema only in the authorized staging environment after the operator and recovery gates above are recorded.
3. Verify all controls are default-off, no contexts or sponsor modules are exposed, and ordinary Explore/search/editorial pages match the editorial-only behavior.
4. Use clearly marked test-only fixtures in a separate test database. Prove sponsor/campaign/creative review, revisions, booking conflict rules, half-open intervals, pause/expiry, CSRF, private-report authorization, event replay/tamper/open-redirect rejection, no-store behavior, and corrected/withdrawn/source-permission invalidation.
5. Use no provider call, payment, actual outreach, or billable impression claim in the local acceptance pass. A server render is an operational observation only.
6. The designated reviewers read the final notice, placement disclosure, event fields, cookie behavior, retention and deletion path. Resolve their consent/lawful-basis decision in their own approval record; do not infer approval from the source setting.
7. Complete actual desktop/mobile, keyboard and screen-reader browser checks. Exercise disabled, empty, ineligible, paused, expired, withdrawn, publication-suspended, and redirect behavior.
8. Only after Q05 acceptance and a separate explicit release decision may the owner change deployment deny or application controls. Observe a pre-agreed period with explicit error, stale-context, event replay, privacy, editorial-independence, and rollback thresholds. Set the duration before the run; this plan does not assume a 14-day period.
9. Rehearse rollback: restore deployment deny first, stop commercial job admission, disable the surface, ensure stale modules disappear, preserve audit/measurement evidence, and verify ordinary editorial pages remain available.

## Acquisition and programmatic prerequisites

- Do not choose or add a second social/news backend until a named candidate has documented dedicated access, current rights, quotas, supported server interfaces, useful energy/food coverage, fixed egress, response/time/byte/process limits, and tombstone/correction behavior. Existing Agent Reach remains YouTube metadata-only and its RSS acceptance gate stays narrow.
- Do not add programmatic script, iframe, pixel, slot, targeting key, or CSP origin until a provider and account are selected and eligible; legal/privacy owners accept geography and consent; the data map, slots, browser requests, reconciliation units/currency/timezone, offline and adblock states are specified.
- Do not let public requests enqueue collectors or invoke an LLM. Context rebuilds are bounded scheduled work under the commercial controls. Optional AI stays disabled until provider-transfer review, route and budget acceptance are recorded.

## Rollback and evidence

Rollback begins with COMMERCIAL_DENY true and the global/surface switches off. Pause job admission, invalidate or allow expiry of commercial context projections, retain redacted audit and uncertain delivery evidence, and preserve ordinary editorial routes. Prefer a forward-compatible correction to an unreviewed destructive downgrade. Record image digest, schema level, backup ID, operator, timestamp, observed effects, and the final release/rollback decision in the acceptance register.
