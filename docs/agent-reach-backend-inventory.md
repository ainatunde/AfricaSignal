# Agent Reach backend inventory

Task: A00. Inspected against source baseline dfa16550904b36a6990be6d4cd644b4fa7a7af4c in the isolated growth worktree.

## Existing working controls

- The bridge is a separate HTTPS runner with a bearer token, durable SQLite task state, idempotent task IDs, bounded concurrency, deadlines, cancellation, stale-generation fencing, and result retention.
- The application checks its deployment deny, workload switch and schedule, endpoint/token health, rolling task quota, active-task limit, and runner-reported capacity before submission. Worker handlers recheck before dispatch, poll, cancel, reconcile uncertain results, and discard stale generations.
- Search requests are accepted by the runner only when the YouTube backend is enabled and its API key file is readable. The runner's search quota is bounded to 1–100 calls per UTC day (default 10).
- The runner's egress proxy currently permits only www.googleapis.com:443; the search client disables environment proxies and redirects, uses bounded timeouts, and caps provider responses at 256 KB.
- Search is metadata-only. It does not fetch candidate pages, transcripts, or attachments, and does not publish or notify.

## Existing backend boundary and gaps

- runner/agent_reach_runner/backend.py implements only youtube_data_api_v3, fixed to YouTube Data API search.list.
- runner/agent_reach_runner/schemas.py exposes one capability, public_search_metadata; health reports only the YouTube backend. There is no provider-neutral backend registry or arbitrary endpoint field.
- The runner store persists task request/result JSON and daily quota counters. It has no normalized native content ID, raw payload hash/reference, backend release fingerprint, or cross-task candidate uniqueness.
- AgentReachCandidate stores the returned URL, title, optional summary/publisher, platform/backend, provider publication time and retrieval time. It does not bind a native post ID or retained source-payload reference/hash.
- Human review remains deliberately narrow: acceptance requires an existing active RSS news source, current permission to collect, and a candidate URL on that source's site. Accepted candidates enter the existing document-processing queue; Agent Reach itself cannot create sources, evidence, assessments, or publication.
- The current runner image pins Python and dependencies, runs as a non-root user, has no application database credential, and receives only its task database and explicit secret files.

## Qualification decision

No second backend is selected by this inventory. Repository code proves that the existing path is YouTube-specific; it does not prove current dedicated access, rights, account eligibility, or useful energy/food coverage for another provider. Those facts must be established for one named backend before A01 can freeze a network contract. The current runner environment and secret-file contents were not inspected.

Do not add a generic social CLI, broaden the egress allowlist, or treat social posts as RSS articles. A selected backend must have a fixed host/path and protocol, reviewed terms and access, bounded authentication and quotas, a dedicated egress rule, safe result normalization, and a human-useful path consistent with the existing RSS permission gate. If no backend meets those conditions, A02–A06 remain blocked rather than inventing an integration.

## Sources inspected

- runner/agent_reach_runner/backend.py
- runner/agent_reach_runner/schemas.py
- runner/agent_reach_runner/server.py
- runner/agent_reach_runner/egress_proxy.py
- runner/agent_reach_runner/store.py
- runner/Dockerfile and runner/requirements.lock
- src/africasignal/agent_reach/client.py
- src/africasignal/operations/agent_reach.py
- src/africasignal/jobs/handlers/agent_reach.py
- src/africasignal/models/ops.py
- src/africasignal/sources/permissions.py
- docs/agent-reach-runner-contract.md
- tests/unit/test_agent_reach_runner.py
- tests/integration/test_admin_ops.py and tests/integration/test_admin_ops_security.py (existing console access and CSRF patterns)