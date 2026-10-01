# Data protection notes (AS-043)

Written 30 September 2026 (updated for the retention, ledger, export and kill-switch changes of the privacy-gaps pull request) to go with the draft privacy notice, terms and correction policy. **This
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
| Email address, created time, verified time | `app_user` | Anyone who enters an address at `/signin` | Yes | Until the holder deletes the account; an address never verified is deleted after 30 days by `apply_retention` |
| Sign-in link and session fingerprints, expiry, used/revoked time | `login_token`, `session` | Account holders | Yes, but only SHA-256 of the secret | Validity 15 minutes and 30 days; rows deleted 30 days after they stop working (`apply_retention`), or with the account |
| Follows, preferences (place ids, topics), notifications | `follow`, `preference`, `notification` | Account holders | Yes | Until deletion |
| Weekly email consent and its time | `app_user.digest_opt_in`, `digest_opt_in_at` | Account holders | Yes | Until deletion |
| Email opened (tracking pixel) | `event` name `digest_open`, `user_id`, week | Readers who opted in | Yes (user id) | 13 months |
| Outgoing sign-in, correction and digest emails | `outbox` (payload has display text, never the address); provider's own records | Account holders | The address is read from the account when sending | Outbox rows of a deleted user are deleted; provider retention unknown |
| "Was this useful?" | `feedback` | Visitors | Visitor code; user id if signed in | `apply_retention` removes text, contact, visitor code and account link after 24 months (setting `feedback_retention_months`) |
| Error report text (2,000 characters), optional typed contact email | `feedback` | Visitors | Visitor code, user id if signed in, email if typed | Text, contact, visitor code and account link are removed after `feedback_retention_months` (default 24, console Settings > Privacy and retention); the row stays. Account deletion unlinks and scrubs the address at once |
| Client address for rate limits | Process memory only, salted hash that changes on restart | Visitors | No | Seconds to an hour |
| Operator accounts, audit log | `operator`, `audit_log` | Operators | Yes | Not covered by the reader-facing notice |
| Deletion ledger: keyed fingerprint of the address (not the address) and date of each account deletion | `account_deletion`; `deletions/` in the app bucket | People who deleted an account | Indirectly: whoever holds `SECRET_KEY` can test a known address against it | Backup retention plus 2 days, then pruned |
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
| Access and copy | `/account/export` (JSON): account, preferences, follows, notifications, feedback (with the contact email and visitor code stored with it), and the events recorded while signed in (time, kind, situation, `ref`, visitor code). Events from before sign-in carry only the visitor code and cannot be tied to the account |
| Erasure | `/account` delete, immediate hard delete; feedback kept but unlinked and scrubbed; events keep only the anonymous code. The deletion is written to a ledger so a restored backup deletes the account again (section 8); otherwise backups expire on their own |
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
| Precise position retained by a component we do not control (proxy, host, CDN) | Keep bodies out of any proxy or WAF logging; write this in the hosting checklist (`launch.md` S5) |
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
10. Is a keyed fingerprint of a deleted account's address, kept for the backup retention period
    plus two days so a restore can delete the account again, acceptable (section 8)?

## 8. After restoring a backup: applying deletions again

**Problem.** Account deletion is a hard delete. A backup made before the deletion still holds the
account, so restoring it brings the person back, with their email address and follows, and the
weekly digest could email them again. That would break the promise that deletion is immediate and
final.

**What the code does.** Each account the holder deletes is written, in the same transaction, to
`account_deletion`: an HMAC-SHA256 fingerprint of the lower-cased address under a key derived from
`SECRET_KEY`, and the time. The address itself is not kept. A job (`mirror_deletions`, queued at
once) also writes the entry to object storage, at `deletions/<date>/<fingerprint>.json` in the
app's bucket, because a database restore rolls the table back to the day of the dump and would lose
exactly the entries it is needed for. The daily `apply_retention` job copies any entry still
pending and prunes entries older than "Days to keep dumps" plus 2 days (no dump that could hold the
account is left after that).

Accounts deleted by the daily retention job (never verified for 30 days) are not in the ledger: a
restore brings them back and the next daily run deletes them again.

**Procedure after any restore** (disaster recovery or a drill against a copy that will receive
traffic). Run it after `restore.sh` has finished and **before** the `web`, `worker` and `scheduler`
services start, or a digest could go out to a deleted account in the gap:

```sh
docker compose up -d db
# ... restore.sh as in docs/runbook.md section 4.1, step 3 ...
docker compose run --rm --no-deps web python -m africasignal.admin reapply-deletions
docker compose up -d
```

It prints `Deleted N account(s) that were deleted after the backup was taken.` It reads the ledger
table and the copies in object storage, deletes each account whose address matches an entry and
that was created at or before the deletion (someone who signed up again afterwards is left alone),
and writes the entries back to the table. It is safe to run twice. It needs the same `SECRET_KEY`
as the old installation, which the runbook already requires for the console's saved secrets. It
also needs the app's object storage settings (console Settings, or `S3_*` variables) so it can read
`deletions/`; with none configured it warns and uses only the table, which has nothing newer than
the dump.

**If the app bucket is lost as well,** restore it first from the backup bucket's `objects/` copy
(`restore.sh --restore-objects`, which also brings back `deletions/`), then run the command. The
copy is made nightly, so deletions in the last day before the loss are not covered. For those
there is no record: check the contact mailbox for deletion requests and errors since the dump and
repeat them by hand.

**Not covered.** Deletions of rows other than accounts (for example a feedback report removed by
an operator on request) are not replayed. The backup bucket's `objects/` copy is copy-only (it
never deletes), so ledger entries stay there after they are pruned from the app bucket; delete
`objects/deletions/` entries older than the dump retention by hand if that matters to counsel.
Question 10 for counsel: is a keyed fingerprint of a deleted address, kept for about 32 days,
acceptable for this purpose.

**Test it in the restore drill** (`docs/runbook.md` section 4.3): before the backup, create an
account with a throwaway address; take the backup; delete the account on the site; restore into the
drill database; run the command against it; check the account is gone.

`docs/runbook.md` (backup and restore pull request) should link here from section 4.1 (between
steps 3 and 4) and section 4.3 (after step 2).
