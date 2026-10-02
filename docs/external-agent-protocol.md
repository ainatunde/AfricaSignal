# External-agent protocol v1

This contract lets AfricaSignal call a separately operated service that offers bounded research or text-processing tasks. It is distinct from the model-provider router and the fixed Agent Reach discovery runner. AfricaSignal currently implements only this exact protocol; it does not adapt arbitrary agent APIs or provide general tool execution.

All requests use HTTPS against a configured public DNS origin. The origin cannot contain credentials, a path, query, or fragment. AfricaSignal pins public DNS results for each request, disables environment proxies and redirects, and limits response bytes and network timeouts. Deployment egress policy remains an independent control and must be qualified in staging.

Each request includes a bearer token. The token is write-only in the admin UI and encrypted in the database under a purpose-derived application key. Rotating the token clears the prior health result and requires a new check. The health probe uses the same authentication and origin restrictions as task calls.

## Health check

GET /healthz returns JSON with this shape:

~~~json
{
  "status": "ok",
  "protocol_version": "africasignal.external-agent/1",
  "agent_id": "review-agent",
  "capabilities": ["research"],
  "enforced_limits": [
    "allowed_domains",
    "idempotency_key",
    "max_cost_per_task_usd",
    "max_output_bytes",
    "max_spend_per_day_usd",
    "max_steps",
    "timeout_seconds"
  ],
  "max_concurrency": 1
}
~~~

The identity must equal the AfricaSignal profile ID. Capabilities must be a subset of the operator-approved purposes. The listed limit names must include every limit required by v1, and concurrency must be between 1 and 4. AfricaSignal also applies its own lower concurrency bound.

Health is a service declaration, not proof of independent enforcement. A successful response makes a disabled profile eligible for operator enablement; it does not qualify that agent for production. Operators must verify the deployed service, its actual data access, domain enforcement, spending controls, privacy terms, and cancellation behavior before a real pilot.

## Submit and idempotency

POST /v1/tasks accepts JSON and an Idempotency-Key header. Both carry the same stable key. Repeating an identical request with that key must return the original task and must never create a second billable execution. Reusing the key with different content must return a conflict. The remote service must retain that mapping long enough for task reconciliation and AfricaSignal's retention period.

~~~json
{
  "protocol_version": "africasignal.external-agent/1",
  "task_id": "123",
  "idempotency_key": "africasignal:external-agent:review-agent:...",
  "purpose": "research",
  "objective": "Summarize public grid reliability reporting",
  "data_classification": "public_metadata",
  "allowed_domains": ["example.org"],
  "limits": {
    "max_steps": 4,
    "timeout_seconds": 90,
    "max_output_bytes": 4096,
    "max_cost_per_task_usd": "0.10",
    "max_spend_per_day_usd": "0.50",
    "deadline_at": "2026-10-02T01:45:00+00:00"
  }
}
~~~

The operator sends only the public objective, approved purpose, exact domain list, and bounded task limits. AfricaSignal does not forward evidence documents, reader records, private source text, application credentials, or publication authority.

The service returns HTTP 200 or 202 with a task response:

~~~json
{
  "protocol_version": "africasignal.external-agent/1",
  "task_id": "123",
  "external_task_id": "remote-456",
  "status": "queued"
}
~~~

Allowed statuses are queued, running, succeeded, failed, cancelled, and expired. A successful result includes result_text; other statuses must omit it. Optional usage fields are input_tokens, output_tokens, and reported_cost_usd. AfricaSignal labels these values as unverified and never uses them to release a reservation or claim actual billing.

## Status, reconciliation, and cancellation

- GET /v1/tasks/{external_task_id} returns the current task response.
- GET /v1/tasks/by-idempotency/{idempotency_key} returns the task or 404. A 404 must mean no task was accepted for that key; services with eventual consistency must not return a false 404.
- DELETE /v1/tasks/{external_task_id} requests idempotent cancellation. It may return 204 when cancellation is complete, or 200/202 with a task response. A task still running after a cancellation response remains cancellable and is checked again.

Submission timeouts, server errors, and rate limits can have ambiguous outcomes. AfricaSignal looks up the same idempotency key before any retry. It retains reserved cost while an outcome remains unknown. If a profile or workload is disabled during an ambiguous outcome, reconciliation preserves cancellation intent and cancels any task the remote service did accept. Late remote results remain untrusted review material.

Task responses and health responses must be JSON, match the protocol version and task identity, and stay within the configured output and response byte ceilings. AfricaSignal rejects redirects and does not follow URLs supplied in objectives or results. Agent output is escaped in the console and cannot write evidence, change controls, approve source rights, or publish.

## Qualification boundary

Local task and reservation limits protect AfricaSignal admission, but an external service can ignore every field unless its implementation and deployment enforce them. Remote usage is not a billing source. A local reservation is a conservative accounting guard only when an operator has verified that the service honors the declared ceilings and billing model. Until then, keep the profile disabled and use inert test credentials and a non-production service in staging.
