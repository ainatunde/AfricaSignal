# AfricaSignal visitor-page Guardian handoff

Date: 17 October 2026. Implementation on `codex/priority-transfers`, based on `7b14576089de4e0450012c0b91909a2cbbbc71f7`. AfricaSignal has no deployment yet; the owner has not selected its domain. This handoff qualifies the implementation for review, not a public launch.

## Implemented

- A scheduled `check_public_pages` job checks a configured public origin every five minutes. Each run makes at most five bounded, read-only GET requests through home/coverage, Explore, a selected item, its situation and history. Redirects, ambient proxy settings and cookies are disabled; response size and request time are bounded.
- Results persist in `guardian_check_run` with a 30-day retention window. `admin/alerts` shows recent page checks. Two consecutive failures open the existing `ops.alert.*` incident; two consecutive passes resolve it. Missing route data is recorded as `unknown`, not healthy.
- For situation and history routes, the checker compares visible version/status/fact rows with the currently eligible assessment. Attribute-name normalization matches the templates' `data-guardian-version-id` and `data-guardian-current-version-id` fields.
- Visitor failures stay escalation-only. AfricaSignal's existing Guardian repair remains limited to permission-checked source retries; the visitor probe does not invent a safe repair target from a generic page failure.
- Situation/history discovery accepts underscores in real commodity slugs such as `price-pms_litre-ng-la`. A regression fixture uses this route.

## Validation

- Focused parser/origin/threshold and local HTTP journey tests pass. The HTTP journey follows five routes, validates the current situation's visible fact and version, verifies history version, confirms the request bound, and confirms a cookie set by the first response is not sent on later requests.
- The full unit suite completed with 1,248 passed and 4 skipped; one existing DNS resolver timeout test failed only in the full-suite run. Its isolated rerun passed. This remains a non-reproducible suite interaction, not evidence about the visitor-check changes.
- The new `0072` migration passed an isolated SQLite DDL upgrade/index/downgrade check. Its `observed` column uses PostgreSQL JSONB on PostgreSQL and portable JSON for the disposable SQLite migration test.
- Ruff on the repository, Ruff formatting on the changed Python files, and mypy over `src` passed.
- A fresh disposable PostgreSQL 16 / PostGIS 3.4.3 database completed the entire Alembic chain through `0072`.
- The real database-backed application served seeded NBS petrol assessments. Chromium desktop (1440 x 1000) and mobile (390 x 844) journeys through home, selected Explore, situation and history returned 200, displayed the expected price/source, and had no horizontal overflow or JavaScript errors. Separate no-data routes also passed.
- The actual Guardian followed the data-bearing Explore/situation/history journey and verified the current version and visible facts. Two checks against a closed loopback port opened incidents; two healthy runs against the app resolved them.
- The final Linux container run passed all 77 selected worker, public-web, visitor-Guardian and migration tests, including real SIGTERM shutdown. Windows `Popen.send_signal(SIGTERM)` uses `TerminateProcess`, so that POSIX-specific test is explicitly skipped there; graceful worker-stop behavior remains covered independently.
- Release CI repair updates stale synthetic explanation answers to retain measurement units and comparison baselines, regenerates their JSONL, and checks that removal of approved reuse rights withholds the assessment. Production validation rules are unchanged.

## Release and deployment

- Review the PR and its current CI result before release. The unrelated local crowd-pricing plan is excluded from this change.
- Deploy only after the owner supplies the domain and infrastructure. Configure `GUARDIAN_PUBLIC_BASE_URL` to the intended origin; verify deployed SHA/schema, scheduled results, independent dead-worker monitoring and operator recovery on that installation.
- No production service, DNS record, external publication or automatic repair expansion was created by this qualification. Disposable databases and test services are removed after evidence collection.

Keep public checks read-only and repair permission-checked source retry only. Do not enable a broader automatic repair from visitor findings without a separately qualified invariant and recovery proof.
