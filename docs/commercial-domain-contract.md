# AfricaSignal commercial domain contract

Date: 2 October 2026. Task: F03. Status: v1 contract for local implementation; no provider, advertiser, payment, or public activation.

This contract follows F01 editorial invariants and F02's `/explore` surface decision. New contracts below are AfricaSignal-owned. They do not describe existing imports. Implementation tasks must include the exact changed-file list and current hashes before coding.

## Version 1 scope

- Direct, manually agreed sponsorship on the topic-filtered Explore view only.
- Surface scope is `explore_topic` plus `energy` or `food`; no place, reader, account, or search-query targeting.
- One exclusive booking may be active for a topic at a time. A booking can carry one approved immutable creative version.
- Context matching is deterministic. AI-assisted classification and report prose are optional scheduled jobs in the existing budgeted model path; they never make editorial decisions or publish a creative.
- The initial manual fee is an explicit NGN integer in kobo. No FX, invoice, settlement, refund, payment provider, impression guarantee, or automatic pricing.
- Programmatic demand, client-side viewability, behavioral segments, reader profiles, cross-product exchange, and advertiser self-service are out of scope.
- The global, Explore-sponsorship, and context-AI switches are versioned controls, all default off. Programmatic gets no operator switch until its separately reviewed adapter task.
- The `commercial` workload is default off, scheduled daily 01:00-04:00 Africa/Lagos, concurrency 1, maximum 10 items per run. Public sponsored rendering, once implemented and separately enabled, remains available outside processing windows.
- The deployment-owned `COMMERCIAL_DENY` flag defaults true and blocks every commercial capability. Admin switches cannot clear it.

## Typed input and output rules

All command/query DTOs are Pydantic models with `extra="forbid"`. Returned projections are immutable. Public input is bounded and validated before service calls; no request accepts a command, arbitrary URL, HTML, SQL fragment, provider name, shell argument, or unbounded list.

Common types:

```python
Topic = Literal["energy", "food"]
PublicSurface = Literal["explore_topic"]
SponsorStatus = Literal["pending_review", "approved", "paused", "retired"]
CampaignStatus = Literal["draft", "approved", "active", "paused", "ended"]
CreativeStatus = Literal["draft", "approved", "rejected", "withdrawn"]
BookingStatus = Literal["draft", "approved", "active", "paused", "ended"]
Suitability = Literal["eligible", "restricted", "unknown", "invalidated"]
DeliveryMetric = Literal["eligible_opportunity", "server_render", "click"]
```

`NGN` is the only supported first-release fee currency. `agreed_fee_minor` is an integer number of kobo or `None` until a fee is agreed; floats and implicit currency conversion are forbidden. A future currency expansion needs a separately versioned currency/exponent table and finance review. Aggregates never sum unlike currencies.

All intervals are timezone-aware instants normalized to UTC in `timestamptz`. Booking eligibility is half-open: `starts_at <= at < ends_at`; require `starts_at < ends_at`. Operator displays use an explicit Africa/Lagos timezone. There is no implicit renewal.

## Storage projections

Use the repository's SQLAlchemy `Base`, model import registry, Alembic conventions, and PostgreSQL integration environment. Do not execute a live migration. Store only necessary business contact data; keep it out of public DTOs, logs, and routine audit snapshots.

| Entity | Version 1 fields and semantics |
|---|---|
| `CommercialControl` | Singleton row with global, Explore-sponsorship, and context-AI booleans; positive revision; updater and timestamp. Every switch defaults false. Programmatic is not operator-controllable in v1. |
| `Sponsor` | `id`; reviewed `public_name`; approved HTTPS `website_url`; minimal private `contact_email`; status; revision; created/updated timestamps and operator IDs. No scraped lead list or outreach state. |
| `Campaign` | `id`; `sponsor_id`; internal name; status; revision; currency=`NGN`; nullable `agreed_fee_minor`; optional agreement reference; creator/approver IDs and approval time. Fee edits invalidate approval and require reapproval. |
| `CreativeVersion` | `id`; campaign ID; monotonically increasing version; bounded reviewed plain text; optional local asset key and alt text; reviewed HTTPS destination; status; reviewer and timestamps. An approved version is immutable; an edit creates a new version. No remote creative URL, HTML, or script. |
| `PlacementBooking` | `id`; campaign ID; surface=`explore_topic`; topic; `[starts_at, ends_at)`; approved creative-version ID; `exclusive=true`; status; revision; creator/approver IDs. No location or user scope. |
| `DeliveryEvent` | event/metric schema version; booking, creative, and booking revision; metric; received time; optional UTC period; one-way deduplication hash; validity/rejection reason code. No account ID, visitor cookie, IP, user agent, raw URL, raw query, or advertiser-supplied event payload. |
| `DeliveryAggregate` | booking/creative/surface/topic; metric definition version; UTC day; count; completeness state; rebuild timestamp. Counts are derived from accepted events and can be rebuilt while raw events remain. |
| `ContentContext` | assessment-version ID and content hash; bounded canonical topic tags and public content references; taxonomy/classifier versions; suitability; reason codes; expiry; invalidation time/reason. It stores no duplicate editorial conclusions and never writes back to evidence or assessment rows. |
| `CommercialDraft` | kind; input references/hashes and revisions; deterministic facts; optional AI text with model/prompt version and cited fact IDs; draft/review/accepted/rejected state; revision; operator decision. It cannot reserve inventory or activate a campaign. |
| `ProviderDeliveryImport` | reserved for the later programmatic task: provider/account/period/import identity, checksum, explicit currency/units, reconciliation state and variance. No provider call in version 1. |

Apply foreign keys, bounded lengths, nonnegative minor-unit checks, unique version/idempotency keys, and revision checks in the schema/service where enforceable. The first migration is additive. Do not rename or alter editorial tables or enums.

## State and booking concurrency

Sponsor transitions: `pending_review -> approved|retired`, `approved -> paused|retired`, `paused -> approved|retired`; `retired` is terminal. Campaign transitions: `draft -> approved|ended`, `approved -> active|paused|ended`, `active -> paused|ended`, `paused -> approved|ended`; `ended` is terminal. Creative approval/rejection is review-gated; withdrawal is terminal. Every mutable aggregate has an integer revision and every operator command supplies `expected_revision`.

Approval and activation require an approved sponsor, approved current creative, valid dates, valid destination, current eligible content context, the surface/global commercial switch on, no deployment deny, and no conflicting active exclusive booking. Recheck all conditions at activation and each render/redirect. Pausing, expiry, withdrawal, or global disable takes effect on the next request; do not rely on a periodic cleanup job.

Serialize booking creation/activation by taking a PostgreSQL transaction-scoped advisory lock for the canonical `(surface, topic)` scope, then checking overlapping active intervals under that lock. All code paths must use that same lock. The interval predicate is `existing.starts_at < requested.ends_at AND requested.starts_at < existing.ends_at`. Never hold this lock over network/model calls. Tests must use concurrent PostgreSQL sessions; SQLite is not acceptance for this race.

## Service signatures and ownership

The following interfaces are the v1 contract, replacing the looser proposals in the planning handoff:

```python
from datetime import datetime
from sqlalchemy.orm import Session
from africasignal.models import Operator


def configure_controls(
    session: Session,
    operator: Operator,
    *,
    change: CommercialControlChange,
) -> CommercialControl: ...


def commercial_state(session: Session, *, at: datetime) -> CommercialControlState: ...


def eligible_placement(
    session: Session,
    *,
    surface: PublicSurface,
    topic: Topic,
    item_code: str | None,
    at: datetime,
) -> PlacementDecision: ...


def record_delivery_event(
    session: Session,
    *,
    envelope: DeliveryEnvelope,
    received_at: datetime,
) -> EventReceipt: ...


def load_context_input(
    session: Session,
    *,
    content_ref: ContentRef,
) -> ContextInput | None: ...


def store_context_decision(
    session: Session,
    *,
    input_ref: ContentRef,
    decision: ContextDecision,
) -> ContentContext: ...


def match_sponsors(
    session: Session,
    *,
    context_refs: tuple[ContentRef, ...],
    at: datetime,
) -> tuple[MatchProposal, ...]: ...


def draft_package(
    session: Session,
    operator: Operator,
    *,
    input_refs: tuple[ContentRef, ...],
    expected_revision: int,
) -> CommercialDraft: ...
```

All referenced DTOs are new, bounded, immutable, and extra-forbidden. `ContentRef` carries an assessment-version ID and expected content hash; the service reloads and validates current public status instead of trusting caller-supplied status, severity, tags, or display text. `PlacementDecision` is an immutable projection with either an eligible creative/booking revision or a safe reason code; it contains no private sponsor contact or fee. `MatchProposal` explains deterministic category matches and never includes a fit percentage.

Services receive the caller's existing SQLAlchemy `Session`, flush their own writes, and never commit or roll back it. The caller owns the transaction and HTTP error mapping. Mutations use expected revisions and write redacted audit rows in the same transaction. Public rendering must not fail because a reporting counter failed; a failure suppresses the commercial module and leaves editorial output available.

For any AI work, first persist or enqueue a bounded snapshot/job, release database locks, then call the existing `build_adapter(session).complete_json(...)` route with purpose `commercial_context` or `commercial_report`, prompt version `commercial_context_v1` or `commercial_report_v1`, a bounded schema, and the current `job_id`. The configured route remains Anthropic/OpenAI only and uses the existing budget reservation. Do not make a provider call while holding booking, settings, or content locks. Persist the result only if the input hash, assessment revision, job fencing token, workload revision, and switches still match. A stale/late result is discarded. No public request invokes an LLM.

## Event, idempotency, and reporting contract

Version 1 records eligible opportunity, server-render, and validated-click metrics only. A server render is not a client-observed view, a human impression, a viewable impression, or a billable quantity. Client view/viewability and provider-billed metrics are unavailable in this pilot and must be displayed as unavailable, not zero. The manual fee has no impression guarantee.

A click URL contains a short-lived signed token bound to the booking ID, creative-version ID, booking revision, event kind, and expiry. The raw token is never stored. Store only its one-way dedupe hash; the unique constraint makes concurrent replay count once. Reject expired, forged, cross-booking, wrong-revision, paused, ended, or withdrawn tokens and revalidate the stored destination before redirecting. The token carries no reader ID or persistent profile. Do not accept a destination from the click request.

Invalid and duplicate submissions return bounded reason codes; do not retain raw request bodies or identifiers. Raw event rows are retained for 30 days. Daily aggregates are retained for 24 months, with explicit completeness and metric-version fields. Rebuilding is supported only for periods whose raw rows remain; older aggregate corrections use audited adjustment rows and never rewrite a claimed human impression. B04 must validate these choices and event/token constraints before any route is added.

A report gives metric definition, UTC/Lagos display period, denominator, attribution, completeness, and source. Server renders and clicks are operational observations only. No report states causal lift, audience size, forecast, or money earned unless independently supplied and reconciled. Provider quantities, if later added, remain separate from AfricaSignal-observed events.

The job kinds owned by the commercial workload are `commercial_context_rebuild`,
`commercial_ai_classify`, `commercial_package_draft`, `commercial_report_export`, and
`commercial_report_narrative`. `commercial_ai_classify`, `commercial_package_draft`, and
`commercial_report_narrative` are also blocked by the existing `ai` workload. F04 registers these
admission mappings before any handler can enqueue them; each handler is added only by its later
reviewed task. No unhandled kind is enqueued.
## Authorization, jobs, and failure behavior

- Commercial reads use existing signed operator sessions. All sponsor, campaign, creative, booking, fee, approval, activation, report-export, and switch mutations require an enabled `admin` operator, CSRF/origin protection, expected revision, and audit. `editor` is read-only in the commercial console. There is no advertiser self-service.
- Default application and surface switches are off. Deployment deny wins. Failure to load effective state, current content, or approved creative fails closed for the sponsor module; editorial content remains available.
- Any new scheduled commercial job is registered in handler, queue admission, workload-kind, retry/dead-letter, and configuration-coverage mappings. The new `commercial` workload uses a reviewed bounded schedule, default disabled, and concurrency 1. Queued/manual/retry paths recheck the switch, deployment deny, window, budget, content revision, and fencing token before dispatch and before storing output.
- AI classification uses a controlled taxonomy and returns bounded category labels/reasons; `unknown` stays ineligible. It cannot infer reader attributes, decide editorial suitability from severity, generate prices, or publish/activate a creative. AI package prose can cite only supplied deterministic facts and is an operator-reviewed draft.
- Domain failures use a typed `CommercialError` code (`not_found`, `revision_conflict`, `invalid_transition`, `booking_conflict`, `invalid_context`, `invalid_destination`, `disabled`, `suspended`, `token_invalid`, `event_duplicate`, `workload_denied`, or `budget_denied`). Public responses expose generic safe copy; details go only to redacted logs/audit.

## Required test obligations before source acceptance

1. Pydantic rejects extra fields, invalid enum states, naive timestamps, negative amounts, oversized strings/lists, and malformed content hashes/tokens.
2. Every status transition and revision conflict is covered; approved creative bytes/URL cannot mutate in place.
3. Two PostgreSQL sessions cannot activate overlapping exclusive bookings in the same scope; half-open boundary intervals do not conflict.
4. Switch-off, deployment deny, publication suspension, stale/withdrawn context, expired creative, failed lookup, and late fenced work all suppress commercial output without changing editorial output.
5. Event replay counts once under concurrency; token tampering, expiry, wrong creative/revision, paused campaign, and request-supplied redirect targets are refused.
6. Reports distinguish opportunity, server render, click, unavailable viewability, and provider-billed quantities; unlike currencies never aggregate.
7. Existing assessment/evidence/search/publication/withdrawal probes continue to pass unchanged. Run only against the isolated disposable PostGIS configuration documented by the repository; never inherit a live `DATABASE_URL`.

Before activation, separately record accountable legal/privacy review, real advertiser agreement, any staging credentials, security/egress evidence, and measured rollout decision. None is implied by source acceptance.

## As-built implementation amendments (2 October 2026)

This section supersedes the proposed sponsor, context, draft, service, and optional-AI rows above where they differ from the source currently delivered.

- Sponsors now carry one fixed category: energy_provider, energy_efficiency, food_retailer, agriculture, general_business, or unclassified. Only the first four map to one topic; general_business and unclassified do not match.
- ContentContext stores exact evidence references and their derived source references. Source references use a PostgreSQL GIN index so permission/activity changes can invalidate projections in the same transaction.
- CommercialDraft currently stores only a package proposal. It snapshots campaign, sponsor, and control revisions, current eligible context identities, booking conflicts, integer NGN minor-unit fee state, deterministic facts, and review status. Approval rechecks those inputs and never creates a booking.
- The implemented match_sponsors service accepts one content reference and returns an immutable result with category match/mismatch reason codes. It returns no fit score.
- The implemented create_package_draft service accepts a bounded PackageRequest with campaign/control expected revisions and a requested interval. review_package_draft requires an expected draft revision.
- Deterministic context rebuild is the only registered commercial job handler. Optional AI purposes, prompts, model transfer, narrative jobs, and creative suggestions are not implemented and remain deferred. Admission mappings alone do not mean a job is runnable.
- Publication/situation changes, source permission or activity changes, evidence-retention expiry, and publication-suspension changes invalidate context rows transactionally. A bounded rotating scan and targeted jobs rebuild only under deployment, global, workload, schedule, and lease-fencing controls.

No section in this contract authorizes provider activation, real advertiser outreach, live migration, or deployment.
