# AfricaSignal Agent Reach runner bridge contract

Status: source implementation only. The separate service and Compose deployment are present in this checkout, but no runner has been built, deployed, or live-qualified.

## Scope and backend

The dashboard switch controls an isolated AfricaSignal discovery bridge. This bridge does not install or execute the upstream Agent-Reach CLI. Its only implemented capability is bounded, metadata-only search through the official YouTube Data API v3 search endpoint. It is not a general browser, social-media, scraping, or arbitrary-agent gateway. New capabilities require separately reviewed adapters, rights checks, resource limits, and metering.

The runner returns only a public YouTube video URL, title, channel name, publication time, and retrieval time. It does not fetch video pages, transcripts, comments, attachments, or media. Results remain untrusted candidate leads. An administrator must map a lead to an active RSS source with current collection permission before AfricaSignal queues ordinary capture.

YouTube documents a separate 100-call daily bucket for search.list and resets daily quotas at midnight Pacific Time. The runner maintains its own counter by Pacific calendar date, defaults to 10 calls, and validates an operator ceiling between 1 and 100. Provider project use by other clients can consume the same bucket, so the provider remains the final authority. This request quota is not a monetary spend ceiling. See [search.list quota impact](https://developers.google.com/youtube/v3/docs/search/list) and [YouTube quota allocation and reset](https://developers.google.com/youtube/v3/determine_quota_cost).

## Transport and authentication

- The application configures an HTTPS endpoint and a dedicated bearer token. The runner requires a token of at least 32 characters from a mounted secret file. Do not reuse an operator or provider credential.
- Health and task routes require the bearer token. Redirects are not followed. The application resolves and pins the configured runner host to public IP addresses, disables environment proxy use, and caps response bodies at 128,000 bytes.
- The runner uses its own HTTPS certificate. The Compose file binds its port to host loopback; an operator-managed reverse proxy must route the application’s approved HTTPS endpoint to that listener.
- The runner container has no application database, publication key, or host Docker socket. The only search egress is an HTTPS CONNECT proxy whose code permits www.googleapis.com:443 and dials a previously validated public IP.
- The proxy allowlist is an application control. The deployment must also qualify network-level egress restrictions, DNS, TLS termination, secret mounting, CPU/memory/process limits, and service reachability before enabling a capability.

## Health

Authenticated GET /healthz returns exactly these fields:

    {
      "status": "ok",
      "protocol_version": "africasignal-agent-reach/1",
      "capabilities": ["public_search_metadata"],
      "backend": "youtube_data_api_v3",
      "max_concurrency": 1
    }

The capability list is empty unless the backend enable flag is true, a valid API-key file is present, and the deployment-owned AGENT_REACH_DENY lock is unset. Health reports configured capability and concurrency; it does not prove source rights, legal/privacy acceptance, editorial quality, live quota, or cost. AfricaSignal records health for 24 hours and binds it to the exact endpoint and token fingerprint.

## Submission and fencing

POST /v1/tasks accepts HTTP 202 and an Idempotency-Key header. The key is stable across retries:

    africasignal:agent-reach:<task_id>

The JSON request contains task_id, idempotency_key, control_generation, config_revision, capability, topic, query, country, deadline_at, max_results, max_output_bytes, and policy. The supported capability is public_search_metadata, topic is energy or food, and country is NG. Both generations must be echoed exactly in every task response. The application uses its workload-control revision for both values.

All collection restrictions must be false: fetch_candidate_pages, fetch_transcripts, download_attachments, and publish_or_notify. The runner rejects unknown fields, deadlines beyond 31 minutes, output limits over 128,000 bytes, result limits over 20, replayed task identities with changed payloads, and work after its capability is disabled.

The runner returns the same external_task_id for an identical task retry. GET /v1/tasks/by-task/{task_id} returns the existing task response or 404 when the runner has no durable record. AfricaSignal uses this lookup before retrying an uncertain submission. A runner restart during a provider request records a visible failed task with an uncertain provider outcome and does not repeat the request automatically. The runner rechecks the deployment-owned AGENT_REACH_DENY lock immediately before starting queued provider work; AfricaSignal independently enforces the same immutable lock at admission, cancellation, reconciliation and result acceptance. Status is queued, running, succeeded, failed, cancelled, or expired. Only succeeded responses may contain results. Results are capped by the submitted max_results.

## Result schema and retention

Each result contains:
- url: absolute public HTTP(S) video URL without embedded credentials
- title: 1–300 characters
- summary: always null for this backend
- publisher: optional channel name, at most 200 characters
- platform: youtube
- backend: youtube_data_api_v3
- published_at: optional timezone-aware provider timestamp
- retrieved_at: timezone-aware collection timestamp

The runner removes provider metadata and the submitted search query 28 days after task creation, retaining only a minimal idempotency tombstone so a delayed retry cannot repeat paid or quota-consuming work. AfricaSignal sets candidate and task retention to 28 days; its cleanup job runs daily. This leaves a buffer under YouTube’s 30-calendar-day limit for non-authorized API data, but the exact deployed cleanup timing must still be verified. See [YouTube API data storage policy](https://developers.google.com/youtube/terms/developer-policies).

## Cancellation

DELETE /v1/tasks/{external_task_id} may return 204 or 404 for terminal cancellation confirmation. It may return 200 or 202 with a task response: queued/running means cancellation is pending; cancelled confirms cancellation; succeeded/failed/expired means work already ended and AfricaSignal will not accept the returned leads. Cancellation is idempotent. Unconfirmed remote outcomes remain visible as outcome_unknown in AfricaSignal for operator recovery.

## Qualification

Before enabling the capability, build and pin the runner image by immutable digest; configure and restrict the YouTube API key; test the actual reverse-proxy route and egress policy; verify Docker resource limits; run cancel/restart and expiry drills; and obtain source-rights, privacy/legal, editorial-quality, and quota/cost acceptance. A passing local unit test or health response is not production acceptance.
