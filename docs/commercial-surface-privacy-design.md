# AfricaSignal commercial surface, privacy, and security design

Date: 2 October 2026. Task: F02. Status: reviewed source design; no provider, advertiser, or public commercial activation.

## Surface decision

The repository has no standalone topic landing route. Its existing `/explore` page provides energy/food tabs and an optional item selection. Use that topic-filtered Explore view as the first candidate direct-sponsorship surface; do not add a new route solely to host an ad. This is a deliberate adjustment to the plan's proposed “topic landing page” after checking the actual route inventory.

Render at most one sponsor module in a separate `<aside>` after the existing topic/item content. It must be labelled `Sponsored`, must not be part of any result list or table, and must not change organic membership, ordering, labels, or pagination. An empty, disabled, expired, or ineligible placement produces no module or placeholder. Do not add ads to the home page, situation/evidence/history pages, place pages, coverage, corrections, legal pages, account pages, or any admin surface in this release.

Context for deterministic matching may use the page's validated topic and item code, plus approved context records for current published content actually represented on that page. It must not use the place/visit cookies, account identity, raw query text, location API input, reader history, severity alone, sentiment alone, or inferred personal characteristics. Context is content geography and taxonomy only. Unknown, stale, disputed, expired, superseded, closed, or withdrawn content yields no match.

During global publication suspension, keep the existing public page and suspension banner. Suppress the sponsor module. This preserves current editorial behavior while failing closed for commercial rendering. The independent commercial/surface switches cannot override the editorial suspension.

## Creative and browser boundary

The first release is first-party, direct sponsorship only. Render operator-reviewed plain text and approved locally hosted image assets. Store and validate the advertiser destination; recheck active campaign, creative revision, and destination before redirecting a click. Use a normal accessible link with an explicit sponsored relationship. Reject arbitrary HTML/JavaScript, remote creative URLs, iframes, pixels, third-party scripts, fonts, SDKs, browser cookies, and open redirects.

Keep the current self-only Content Security Policy. Local assets fit `img-src 'self'`; anchor navigation to a validated advertiser destination does not require widening `script-src`, `connect-src`, `frame-src`, or `img-src`. Do not weaken the separate admin CSP. Public GETs must not dispatch collectors, call an LLM, or make third-party ad requests.

## Measurement and caching

The initial direct-fee pilot has no impression guarantee. Report server-side eligible opportunities and server renders as operational observations, never billable human impressions. If click totals are included, record validated first-party redirect events without visitor/account identifiers; enforce the token, deduplication, rate, retention, and attribution contract in B04 before implementation. No client beacon or viewability claim is included in this first surface. Missing metrics are reported as unavailable, not zero.

Do not join commercial delivery events to `anon_id`, signed-in account IDs, visit-time cookies, page-view identities, or feedback. Do not expose reader or account data to advertisers. Keep page-context selection independent from the existing site analytics middleware.

The current Explore response is publicly cacheable for 60 seconds. Once sponsorship can be rendered there, disable shared/public caching for the sponsor-capable response (including when the placement is empty) unless a separately reviewed invalidation mechanism proves that pause, expiry, correction, withdrawal, and permission changes remove stale sponsor content before it can be served. A later cache optimization requires explicit revision keys and acceptance evidence; the first implementation should be fail-closed.

## Privacy notice and activation gate

The current privacy page says AfricaSignal does not use advertising and that no cookie is used for advertising. The first statement becomes false once sponsorship launches. Before activation, revise the notice to explain direct sponsorship on Explore, the separation from editorial decisions, the fact that sponsors receive no reader data, the absence of third-party ad tracking, and exactly which aggregate render/click measurements are retained. Keep the statement that no cookie is used for advertising only if source review confirms no cookie is used for ad selection or measurement. Do not add a cookie or consent banner as an implementation shortcut.

The current notice also contains an open counsel review of lawful bases and cookie consent. Legal/privacy acceptance is a launch gate: prepare accurate copy in source, but do not activate sponsorship or programmatic ads until the accountable reviewer accepts the updated notice, measurement purpose, retention, and applicable consent requirements. Programmatic advertising, cross-site tracking, behavioral segments, and reader-level targeting remain out of scope.

## Protected source boundaries

- Surface and rendering: `src/africasignal/web/routes/public.py`, `src/africasignal/web/render.py`, and the new Explore module/template boundary. Preserve organic calls in `src/africasignal/web/queries.py`.
- Browser policy: `src/africasignal/web/security_headers.py`, `src/africasignal/web/app.py`, and existing public/admin origin guards. No blanket CSP relaxation.
- Existing analytics and privacy: `src/africasignal/web/analytics.py` and `src/africasignal/web/templates/privacy.html`. Do not reuse reader identifiers for sponsor reporting.
- Editorial invalidation: the F01 publication/version and correction/withdrawal anchors remain authoritative; a cached or delayed commercial context cannot outlive them.

These are integration boundaries, not blanket edit authorization. Exact files and acceptance commands must be supplied per implementation task after F03 finalizes the contracts. External legal and live provider acceptance remains pending; no credentials or activation are part of F02.