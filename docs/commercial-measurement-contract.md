# AfricaSignal commercial measurement contract

Date: 2 October 2026. Task: B04. Status: source contract; event collection and redirect routes are not enabled.

## First-release observations

- Supported measurements are eligible opportunity, server render, and validated click.
- A server render is an operational response observation. It is not a human view, viewable impression, billable impression, or audience count.
- Provider-billed and client-viewability measurements are unavailable in this first-party pilot and must render as unavailable, not zero.
- Events contain booking/creative revisions, metric schema/version, server receive time, one-way deduplication hash, validity code, and a 30-day raw retention deadline.
- Daily aggregates are rebuildable for 24 months. Bucket starts are UTC midnight; reports also label Africa/Lagos display dates and state period completeness.
- No event stores anon_id, user/account ID, cookie, IP, user agent, raw URL, raw query, request body, or advertiser-supplied event metadata. Invalid submissions are answered with bounded reason codes and are not persisted.
- COMMERCIAL_DENY, global/surface switches, publication suspension, current eligible content context, active booking/campaign/sponsor, and approved creative are rechecked at click time.

## Click token

The click token is a purpose-separated HMAC token using the existing signing helper and a random 128-bit-or-greater nonce per rendered link. Its signed payload contains only token schema version, event_kind=click, booking ID, creative-version ID, booking revision, issued-at, expiry, and nonce. It expires after 15 minutes. The raw token is never stored; event uniqueness uses SHA-256 of the token string. Tokens contain no reader or session identifier.

The route accepts only the token path parameter. It ignores and rejects caller-supplied destination parameters. The redirect target is loaded from the stored creative and validated again as HTTPS with a public DNS host, no credentials, query, fragment, or nonstandard port. The service rechecks the token bindings and all live eligibility state before returning the destination. A pause, expiry, withdrawal, revision change, denied switch, suspension, or invalid/stale context suppresses the redirect.

## Rate and retention boundary

Use the existing in-process public request limiter before token verification, with keyed transient client-address hashing and no persistent address storage. It is an abuse guard, not a globally accurate quota. PostgreSQL's unique deduplication hash makes replay single-count under concurrent workers. Do not record rejected request payloads. Retention cleanup removes raw events after 30 days; aggregates are retained for 24 months. Cleanup execution and actual report presentation belong to later tasks.

## Atomic event contract

B05 will validate a bounded DeliveryEnvelope with extra fields forbidden. Click claims are verified before an event insert. Event insertion and deduplication happen in the request transaction; the unique hash resolves races. Failure to record a render/counter suppresses only the sponsor module or event response, never the editorial page. No payment or invoice is derived from these observations.

## Pending external acceptance

A real browser, privacy counsel, legal basis, staging host, advertiser agreement, and rollout observation remain separate gates. No provider account, recipient list, price, or live advertisement is activated by this contract.