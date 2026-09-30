# Launch checklist (AS-043)

Nothing on this list is done by merging code. Each line has an owner and a way to check it. Lines
marked **Decision** need a person to choose; the options and a recommendation are in
[`data-protection.md`](data-protection.md) for the legal ones.

The operator console Settings page (`/admin/settings`) holds the values that have to be filled in:
Operator name, Contact address, Legal pages reviewed, Public address, Email provider, API key and
sender address, backup storage. `python -m africasignal.admin get-setting <key>` reads one on the
host.

## 1. Legal and data protection

| # | Item | Owner | How to check |
|---|------|-------|--------------|
| L1 | **Decision.** Who is the operator (a person or registered company) and what address goes in the terms. Set **Operator name** in Settings. | Tunde | `/privacy` and `/terms` show the name, not `[operator name not set yet]` |
| L2 | **Decision.** A contact mailbox that someone reads and that can meet the 72-hour reply target. Set **Contact address** in Settings. Same address is shown on the crawler page unless `BOT_CONTACT_EMAIL` is set. | Tunde | Send a test message; `/about/bot` shows it |
| L3 | A lawyer who knows Nigerian data protection and media law reviews `/privacy`, `/terms`, `/corrections` and answers every question marked **To confirm** in them. Edit the templates (`src/africasignal/web/templates/{privacy,terms,corrections}.html`) to the lawyer's wording. | Lawyer, then Tunde | `grep -rn "todo(" src/africasignal/web/templates/` prints nothing and `curl -s https://<domain>/privacy | grep -c data-todo` prints `0` for all three pages |
| L4 | Set **Legal pages reviewed** to `yes` in Settings. This removes the draft banner and the `noindex` tag. Do this last, never before L3. | Tunde | Banner gone; `view-source` shows no `noindex` on the three pages |
| L5 | **Decision.** Whether the Nigeria Data Protection Commission requires registration, a compliance audit filing or a data protection officer at the expected user numbers (plan D3). Record the answer and any filing date in `data-protection.md` section 6. | Lawyer | Answer recorded |
| L6 | Sign off the short data protection impact assessment for location handling (`data-protection.md` section 5). | Tunde + lawyer | Sign-off line filled in |
| L7 | **Decision.** Whether the visitor-code cookie (`anon_id`) needs a consent banner in Nigeria or can rest on legitimate interest as the draft says. If a banner is needed it is new work (a ticket), because the page views are recorded before any choice is made. | Lawyer | Answer recorded |
| L8 | **Decision.** Retention for feedback and error reports (they are kept indefinitely today; recommended 24 months, then delete or strip text and contact). | Tunde | Number chosen; ticket for the job if not 'forever' |
| L9 | **Decision.** How deletions are applied again if a backup is restored (see gap G4 below). | Tunde + lawyer | Runbook step written |
| L10 | Data processing terms signed or accepted with the email provider and the hosting and backup providers, and their countries written into the privacy notice (the **To confirm** lines). | Tunde | Countries appear in the notice |
| L11 | **Decision.** The licence, if any, for reuse of AfricaSignal's own assessments and data (terms page, Content and ownership), and who owns the code. | Tunde | Wording in the terms |

## 2. Gaps in the code found while writing the notice

The notice describes what the code does today. These differences between the plan or the notice
and the code need a ticket (or an explicit decision to accept them) before launch. None is fixed in
the pull request that added this checklist.

| # | Gap | Suggested fix |
|---|-----|---------------|
| G1 | An account row is created the moment anyone types an address at `/signin`, before the address is verified. Never-verified accounts, and expired or used `login_token` and `session` rows, are never deleted. | A daily job: delete sessions and login tokens 30 days after they expire, and delete accounts never verified after 30 days. |
| G2 | Feedback, including a typed contact email, is kept forever (see L8). | Retention job chosen in L8. |
| G3 | "Download everything we hold about you" does not include `event` rows (page views and digest opens linked to the account), and the anonymous visitor code is not tied to an account that could ask for it. | Include the account's own events in the export, or say in the notice why not (the draft says they are left out because they are tied to a random code). Counsel to confirm. |
| G4 | Account deletion is a hard delete with no ledger, so after a backup restore the deleted account is back and nobody knows it was deleted. | Either keep a ledger of deletions (a hash of the address and a date; itself personal data, so counsel decides) or make the restore runbook say deletions are replayed from the contact mailbox. |
| G5 | The plan says "no IP address is stored anywhere". The application keeps none, but `uvicorn` logs client addresses and requested URLs by default. The `Dockerfile` now starts it with `--no-access-log`. The reverse proxy or host in front of it has its own logs. | Configure the proxy to omit addresses or rotate logs within days, and write what it does in the notice (the **To confirm** line under Technical logs). |
| G6 | There is no console control for the publication kill switch on the current branch (`setting.publication_suspended` is read everywhere but nothing in the console sets it). | Ticket, or flip the row by hand and test that. |

## 3. Plan launch gates

| # | Item | Status when written | How to check |
|---|------|---------------------|--------------|
| P1 | Every source used at launch has its terms recorded and its permission approved in the console. NBS terms were not reviewed; news feeds are seeded inactive because their terms mostly allow personal, non-commercial use only; NMDPRA has no public route. | Open | `/admin/sources`: only reviewed sources approved; `/coverage` matches |
| P2 | Evaluation gate passed (AS-040). | Other workstream | Gate report in the repo |
| P3 | Restore drill done on staging and recorded in `docs/runbook.md` (AS-041). | Other workstream | Runbook entry with a date |
| P4 | Security review finished with no open high or medium findings (AS-042). | Other workstream | Review notes |
| P5 | Kill switch tested in production: suspend, check the banner and that no email or channel post goes out, resume. | Open; see G6 | Test log with times |
| P6 | An operator is on call for launch week, with the contact mailbox in L2 being read daily. | Tunde | Named person and dates |

## 4. Site and hosting

| # | Item | Owner | How to check |
|---|------|-------|--------------|
| S1 | **Decision.** Domain (plan D7) and hosting. | Tunde | Domain resolves |
| S2 | Set **Public address** in Settings to the real `https://` address, and `BOT_INFO_URL` so the crawler's user agent points at `https://<domain>/about/bot`. | Tunde | `python -c "from africasignal.net.netutil import USER_AGENT; print(USER_AGENT)"` on the host |
| S3 | HTTPS everywhere (the privacy notice says connections use HTTPS); `ENV` is not `development`, so cookies are marked `Secure`. | Tunde | `curl -sI` shows HTTPS; cookies carry `Secure` |
| S4 | Email provider chosen, sender domain verified (SPF and DKIM), API key and sender saved in Settings. A sign-in email and a digest arrive and the unsubscribe link works. | Tunde | Test sign-in; Settings page shows no expected setting missing |
| S5 | `docker compose` (or the host's equivalent) starts the web service with `--no-access-log`, and the proxy's own log policy matches the notice (G5). | Tunde | `docker compose config` shows the web command; proxy config |
| S6 | Backups running to the backup bucket; the privacy notice's backup period matches **Days to keep dumps**. | Tunde | Notice on `/privacy` shows the same number as Settings |
| S7 | The site's social accounts (WhatsApp channel, X) are created and their own privacy settings reviewed; links posted there use `?ref=wa` or `?ref=x` (plan D1). | Tunde | Links checked |

## 5. Language model data

The notice promises that the language model is sent only text from public sources and never
anything about readers. When AS-021 and AS-028 land, read the prompt builders and confirm nothing
from `feedback`, `app_user`, `event` or a reader request reaches a prompt. Check the model
provider's data retention terms and, if the provider keeps prompts, name it in the notice.
