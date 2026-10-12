# AfricaSignal editorial and commercial invariants

Date: 2 October 2026. Task: F01. Status: source contract for the planned commercial subsystem.

This contract protects AfricaSignal's energy and food reporting while sponsorship and contextual
commercial intelligence are added. It is based on the refreshed source baseline at `dfa1655`.
F02 chooses eligible public surfaces; F03 finalizes the service and storage contracts. Neither
decision may weaken the invariants below.

## Authority boundary

Editorial records remain authoritative for sources, source permissions, evidence, claims,
measurements, assessments, assessment versions, publication, corrections, and withdrawals.
Commercial records may refer to a specific published content/version identity and its approved
context projection. Commercial code must not write or influence editorial records, invoke an
editorial decision, or make an unpublished or withdrawn item appear published.

The commercial subsystem is a read-only consumer of editorial state. A campaign, creative,
booking, match, report, or provider result cannot change:

- source permission, provenance, credibility, or collection scope;
- evidence selection, claim support, confidence, measurement, or factual conclusions;
- assessment severity, status, validity, publication time, or publication suspension;
- organic result membership or ordering; or
- correction, expiry, withdrawal, notification, or deletion behavior.

Editorial publication and withdrawal continue to work when every commercial switch is off or the
commercial subsystem is unavailable. A commercial failure must not block an editorial correction or
withdrawal.

## Public presentation

F02 must designate the allowed sponsor surface and explicitly protect assessment conclusions,
evidence and source panels, severity labels, disputed or withdrawn notices, essential controls, and
organic search results. Sponsored material is a separately disclosed module, never an organic result
or a ranking signal. It must not reorder, replace, or suppress an organic result, and it must not use
raw search text or reader history to select an advertiser.

Every sponsored rendering carries a clear `Sponsored` disclosure. Only reviewed text and approved
locally hosted assets may be rendered. No arbitrary HTML or script, fabricated advertiser, sample
revenue claim, or empty ad box is allowed. If a booking is disabled, expired, withdrawn, or
unavailable, the ordinary page remains useful and its editorial content is unchanged.

## Content eligibility and invalidation

Commercial context is attached to an exact content and assessment-version identity, including a
content hash and the taxonomy/classifier versions used. Context is not a replacement for the
editorial record. The first release classifies content, not readers; content geography is not visitor
location. Severity or sentiment alone cannot establish advertiser suitability.

Unknown or disputed suitability, stale or expired content, superseded or closed situations, and
withdrawn versions are commercially ineligible. Eligibility uses the current published assessment
revision; an older cached assessment cannot stand in for it. A correction, withdrawal,
source-permission change, or relevant taxonomy change immediately invalidates the prior commercial
projection. A later scheduled rebuild cannot keep an invalid projection eligible in the meantime.
The correction or withdrawal notice remains visible according to editorial rules even when its sponsor
module is suppressed.

The existing global publication-suspension control keeps public pages available with a banner and
stops new publication; it does not change the status of already published content. F02 must specify
commercial behavior in this state. The fail-closed default for the first release is to suppress the
sponsor module during suspension while preserving the page, banner, and editorial content. A
commercial switch cannot override publication suspension.

Matching is deterministic and explainable from reviewed advertiser categories and eligible content
context. Missing or stale inputs produce no match, not a default-positive match or invented fit score.
Operator review is required for package and creative proposals; a proposal does not reserve inventory
or publish an advertisement.

## Acceptance matrix

These are required behavioral tests for the first implementation that connects commercial records to
public or editorial data. F01 records the contract before those records and surfaces exist; it does
not add tests that can only pass by inspecting their own implementation. Add the executable cases to
the corresponding later task packet before integrating that path.

| Scenario | Required result |
|---|---|
| No eligible campaign, or commercial switch off | Organic page and search output match the editorial-only behavior; no empty sponsored module appears. |
| Add, activate, pause, or expire a campaign | Evidence, claims, assessments, publication state, and organic result membership/order remain unchanged. |
| Eligible campaign on an F02-approved surface | A separate, clearly disclosed sponsored module may appear; it does not enter or reorder organic results. |
| Draft, held, stale, expired, disputed, superseded, closed, or withdrawn content | No sponsored match or rendering; existing editorial status and notices remain correct. |
| Global publication suspension | Keep public pages and the existing banner; suppress the sponsor module; do not change editorial content or status. |
| Correction, withdrawal, permission change, or taxonomy invalidation | Old context is ineligible immediately, including through cached or delayed rebuild paths; editorial invalidation proceeds independently. |
| Unknown suitability or missing context | No match. Severity, sentiment, or visitor location cannot turn unknown into eligible. |
| Commercial worker, provider, or rendering failure | Editorial publication, search, correction, and withdrawal behavior remains available and unchanged. |

## Current source anchors to preserve

- Publication decisions: `src/africasignal/assess/publication_policy.py` and
  `src/africasignal/publish/versions.py`.
- Correction and withdrawal propagation: `src/africasignal/publish/invalidation.py` and
  `src/africasignal/operations/assessments.py`.
- Evidence and editorial state: `src/africasignal/models/evidence.py`, `claims.py`, and
  `assessments.py`.
- Public selection and ordering: `src/africasignal/web/queries.py` and
  `src/africasignal/web/routes/public.py`.
- Existing behavioral coverage: `tests/unit/assess/test_publication_policy.py`,
  `tests/integration/test_publication.py`, and `tests/integration/test_publication_console.py`.

The current source has no sponsor, campaign, or commercial module. The paths above are protected
integration boundaries, not blanket authorization to edit them. F02 must settle the surface before
public rendering changes; F03 must settle typed contracts before schema or service code is added.