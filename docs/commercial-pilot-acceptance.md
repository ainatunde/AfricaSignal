# Direct sponsorship pilot acceptance packet

State: prepared for local source acceptance only. No real advertiser, agreed fee, provider, staging host, or live activation is present.

## Default-off acceptance

- The deployment-owned COMMERCIAL_DENY flag defaults true.
- Global commercial, Explore sponsorship, context AI, and the commercial workload default off.
- Legal-page review and commercial privacy-review settings default to unconfirmed.
- No setting change in a test fixture constitutes external acceptance.
- Synthetic test sponsors use example.org domains and test-only fees. They are not real advertiser records or price evidence.

## Local checks

Run the commercial integration modules only with the repository's isolated disposable PostGIS database. The integration fixture downgrades/recreates the schema, so do not point TEST_DATABASE_URL at any shared or production database.

Required behaviors: default state blocks activation; approval is revision-checked; exclusive topic bookings use half-open intervals and PostgreSQL concurrency locks; stale or permission-invalidated contexts fail closed; public disclosure is separate from organic topic content; server observations are not billable impressions; event replay counts once; invalid tokens and request-supplied redirect destinations fail; reports are private and missing data remains unavailable rather than zero; package approval never creates a booking.

## External gate before a real pilot

A named privacy/counsel reviewer must accept the revised notice, measurement purpose, retention and applicable consent conclusion. A named commercial owner must record the actual advertiser and manual terms before any campaign is treated as real. A named editorial owner must accept the separation and suppression behavior. A named operations owner must provide staging, backup, monitoring, egress and rollback evidence.

No invoice, settlement, payment, impression guarantee, client viewability claim, or programmatic delivery is included.
