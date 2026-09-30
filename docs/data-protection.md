# Data protection notes (AS-043)

Written 30 September 2026 to go with the draft privacy notice, terms and correction policy. **This
is an engineer's inventory of what the code does, not legal advice.** A lawyer should check it
against the Nigeria Data Protection Act 2023 and current Nigeria Data Protection Commission (NDPC)
guidance before launch. Section numbers of the Act are left out on purpose: counsel adds them.

The pages themselves are `/privacy`, `/terms` and `/corrections`
(`src/africasignal/web/routes/legal.py` and the templates beside the other pages). The cookie
table and the numbers on the privacy page are built from the constants the code uses, and
`tests/integration/web/test_web_legal.py` fails if they drift.

## 1. What is collected

| Data | Where | Who it concerns | Linked to a person? | Kept |
|------|-------|-----------------|---------------------|------|
| Visitor code (random, 16+ characters) | Cookie `anon_id`; `event.anon_id`, `feedback.anon_id` | Every visitor whose browser is not a bot | Only by whoever holds the cookie; when signed in, `event.user_id` sits beside it | Cookie 1 year; events 13 months |
| Page view and related events: time, page kind, situation, `ref` (`wa`, `x`, `email`, `share`) | `event` | Visitors | As above | 13 months, deleted daily by `prune_events` |
| Place choice and last-visit times | Cookies `place`, `visit_prev`, `visit_cur` | Visitors who pick a place | Browser only; read per request, not stored | 1 year |
| Position from "Use my location" | One request to `POST /places/locate` | Visitors who press the button | No. Resolved to an area and dropped | Not stored or logged (access log off) |
| Email address, created time, verified time | `app_user` | Anyone who enters an address at `/signin` | Yes | Until deletion (see gaps G1 in `launch.md`) |
| Sign-in link and session fingerprints, expiry, used/revoked time | `login_token`, `session` | Account holders | Yes, but only SHA-256 of the secret | Validity 15 minutes and 30 days; rows kept until deletion |
| Follows, preferences (place ids, topics), notifications | `follow`, `preference`, `notification` | Account holders | Yes | Until deletion |
| Weekly email consent and its time | `app_user.digest_opt_in`, `digest_opt_in_at` | Account holders | Yes | Until deletion |
| Email opened (tracking pixel) | `event` name `digest_open`, `user_id`, week | Readers who opted in | Yes (user id) | 13 months |
| Outgoing sign-in, correction and digest emails | `outbox` (payload has display text, never the address); provider's own records | Account holders | The address is read from the account when sending | Outbox rows of a deleted user are deleted; provider retention unknown |
| "Was this useful?" | `feedback` | Visitors | Visitor code; user id if signed in | Indefinite today (L8) |
| Error report text (2,000 characters), optional typed contact email | `feedback` | Visitors | Visitor code, user id if signed in, email if typed | Indefinite today (L8); account deletion unlinks and scrubs the address |
| Client address for rate limits | Process memory only, salted hash that changes on restart | Visitors | No | Seconds to an hour |
| Operator accounts, audit log | `operator`, `audit_log` | Operators | Yes | Not covered by the reader-facing notice |
| Database backups | Backup bucket | Everyone above | Yes | "Days to keep dumps" setting, default 30 |

Not collected anywhere in the application: IP address, user agent, full URL, query strings
(including place-picker search text), device identifiers, third-party analytics or fonts, cookies
from other sites. The pages load nothing from other hosts.

## 2. Recipients

- Operators, through the console (two-factor sign-in, audit log). They can read feedback.
- The email provider (Postmark or Resend; the choice is in Settings): receives the recipient
  address and message text.
- Hosting, database and backup providers: not yet chosen.
- The language model provider: public source text only, by design (see `launch.md` section 5).
- No sale, no advertising, no data brokers.

## 3. Lawful basis as drafted (counsel to confirm)

| Purpose | Basis drafted |
|---------|---------------|
| Account and follows | What the user asked for (contract or request) |
| Weekly and correction emails | Consent, recorded with a timestamp, withdrawable by one click |
| Visitor metrics | Legitimate interest, with minimisation: no IP, no user agent, 13 months |
| Error reports and usefulness votes | Legitimate interest; a typed email is voluntary |
| Abuse limits and security | Legitimate interest |

## 4. Rights and how they are met

| Right | How |
|-------|-----|
| Access and copy | `/account/export` (JSON): account, preferences, follows, notifications, feedback. Events are not included (G3) |
| Erasure | `/account` delete, immediate hard delete; feedback kept but unlinked and scrubbed; events keep only the anonymous code. Backups expire on their own (G4) |
| Withdraw consent | One-click unsubscribe link in every email, or the account page |
| Correction | The account holds only an email address, which cannot be edited; a new address means a new account. Reports can be amended on request by mail |
| Anything else | Contact address; target 30 days (draft) |
| Complaint | To the NDPC, stated in the notice |

## 5. Short data protection impact assessment: location

**What.** The home page offers "Use my location". JavaScript (`static/site.js`) calls the browser's
geolocation only when the button is pressed, and the browser asks permission first. The
coordinates go once, over HTTPS, in the body of `POST /places/locate`.

**Why.** To choose the state and local government area without the reader searching for it.

**How it is handled.** `queries.locate` runs one `ST_Covers` query on place boundaries and returns
the area and state. The route writes nothing: no database row, no event, no cache entry, no log
line. The application's access log is off (`Dockerfile`), and the body of a POST is not in any
default proxy access log. Only the chosen place code is kept, in the reader's own cookie.

**Risks and answers.**

| Risk | Answer |
|------|--------|
| Precise position retained by a component we do not control (proxy, host, CDN) | Keep bodies out of any proxy or WAF logging; write this in the hosting checklist (G5) |
| Position inferred later from the stored place | Only an LGA is kept, in the reader's browser |
| Reader does not understand what is sent | The button says what it does; the notice explains it |
| Misuse by the site | Nothing precise is stored, so nothing can be reused |

**Residual risk.** Low, provided the proxy does not record request bodies.

Sign-off: ______________________  Date: ____________

## 6. Regulator and registration

To be answered by counsel and recorded here: does the expected audience make AfricaSignal a data
controller of major importance under NDPC rules; if so the registration and annual compliance
audit filing dates; whether a data protection officer is needed; and whether the NDPC expects a
filing before launch. Answer: ____________________

## 7. Questions for counsel

1. Is legitimate interest right for the visitor code, and is a consent banner needed?
2. Wording and limits of liability under Nigerian law and consumer protection rules.
3. Takedown route for rights holders, and quoting limits for news and official sources.
4. Transfers outside Nigeria: safeguards for the email provider and any foreign host or bucket.
5. Age rule (the draft says the site is not aimed at under-18s) and whether a parental-consent
   statement is needed.
6. Breach notification duties and time limits to state in the notice.
7. Whether the 30-day target for privacy requests is acceptable, and what identity checks apply.
8. The governing law and dispute clause, and the operator's registered address.
9. Defamation risk now that only prices and official policy changes are published, and what
   review is needed before any allegation about a person or company is ever added.
