# AfricaSignal

A free, mobile-first site that shows people in Nigeria what has changed in the prices and policies
that affect their household budget, with the evidence and its limits stated.

Release 1 covers household energy and food staple prices. See the implementation plan for scope.

## Run locally

```sh
docker compose up --build        # web + PostGIS
docker compose --profile dev up  # also start MinIO (S3-compatible storage)
curl localhost:8000/healthz      # {"ok": true, "db": true}
```

## Develop

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy src
pytest   # needs PostgreSQL 16 + PostGIS; set DATABASE_URL (default: localhost africasignal_test)
```

## Source registry

`python -m africasignal.sources.seed` (run by the compose `migrate` service) upserts
`config/sources.yaml`. Seeded permissions are created **unapproved**; nothing is fetched from a
source until an operator approves its permission. See the notes at the top of `config/*.yaml` for
what is still unverified.

## Operator console

The console lives at `/admin` (password + authenticator code). There is no public sign-up: create
the first operator on the host.

```sh
python -m africasignal.admin create-operator --email you@example.org --role admin
# prompts for a password (12+ characters) and prints the TOTP secret once; add it to an authenticator app
python -m africasignal.admin disable-operator --email you@example.org   # or enable-operator
```

`admin` operators approve source permissions, publish new versions and resume sources; `editor`
operators can view everything and pause a source. Every change writes an `audit_log` row (see
`/admin/audit`). Behind a TLS-terminating proxy, set `ADMIN_TRUSTED_ORIGINS` to the public origin
(for example `https://console.example.org`) so form posts pass the origin check. `SECRET_KEY`
signs the session cookie and encrypts TOTP secrets and the secrets saved under Settings, so
changing it signs everyone out and makes all of those unreadable (recreate operators, re-enter the
secrets). Scripts on the host can read a setting with
`python -m africasignal.admin get-setting backup_s3_bucket` (add `--reveal` for a secret).

## Accounts, feedback and metrics

Readers sign in with an emailed link (`/signin`). The link opens `/signin/verify`, which only shows
a button; the token is spent by the POST behind it, because mail scanners open links. A signed-in
reader can follow situations (`/following`), see in-site notifications, choose the weekly email,
download their data and delete their account (`/account`). `/unsubscribe` works the same way and
also answers a mail client's one-click POST.

Feedback ("Was this useful?", "Report an error") is limited to 10 submissions per visitor per day.
The site records page views, follows, feedback, share clicks and (for readers who opted in) digest
opens as `event` rows keyed by a random `anon_id` cookie. No IP address or user agent is stored,
crawlers and link previews are not counted, and events are deleted after 13 months.

In the operator console, **Feedback** is the inbox (status and a resolution note, audited) and
**Metrics** shows the plan's D1 demand-test numbers by ISO week against the proposed thresholds.
Two numbers come from outside the app and are entered there by an admin: WhatsApp channel
followers and the monthly infrastructure bill.

The email provider, sender, API key and public address are read from the console **Settings** page
on every use (environment variables are the fallback). With no provider configured, development
logs mail and every other environment leaves it waiting in the outbox.

## Load places

```sh
scripts/load_places.sh    # 1 country, 37 states, 774 LGAs and place aliases; safe to re-run
```

Boundaries are geoBoundaries gbOpen for Nigeria (CC BY 4.0, attribution: William & Mary geoLab),
pinned by SHA-256 in `src/africasignal/places/load.py`. Extra names live in
`config/place_aliases.yaml`. Pass `--cities NG.txt` to add GeoNames cities of 50,000 or more.

## Configuration

Only `DATABASE_URL` and `SECRET_KEY` must be environment variables (with `ENV=staging` or
`ENV=production`, startup fails with `RuntimeError` if either is missing). Everything else an
operator can set in the console under **Settings** (Anthropic key, public address, email provider
and sender, evidence and backup storage); an environment variable of the same name in capitals
(`ANTHROPIC_API_KEY`, `PUBLIC_BASE_URL`, `S3_BUCKET`, ...) is used when nothing is saved there.
See `src/africasignal/settings_store.py` for the list.

## NBS price data

Two sources feed the same adapter (`config/sources.yaml`, both seeded unapproved):

- `nbs-elibrary`: the eLibrary page. It lists releases up to **October 2024** only.
- `nbs-microdata`: the NBS microdata catalog, which carries the price watches from 2025 on as ZIPs
  (a PDF plus an xlsx). Newest on 2026-09-30: May 2026 (April 2026 for cooking gas).

Real files from both are saved in `tests/fixtures/nbs` with their URLs, dates and checksums in
`manifest.json`. The parser (`sources/nbs_workbook.py`) reads them as they are; the import rules
(vintages, restatements, the range-check queue) are described at the top of `sources/nbs.py`.
Assessments (`publish/situations.py`) are computed in `assess/price_change.py` and are stored as
drafts until the publication policy decides them.

Email: the provider is chosen behind `publish/email.py`. `console` (logs only) and `fake` are
accepted for development and tests; Postmark and Resend are implemented and have only been tested
against a mocked transport. The provider, API key, sender and public address are saved in the
operator console (Settings) and read on each use, so a change applies on the next send; the
`EMAIL_*` and `PUBLIC_BASE_URL` environment variables are only the fallback.

## Backups

`scripts/backup.sh` (nightly via the `backup` compose profile) dumps the database and copies the
evidence bucket to a separate bucket; `scripts/restore.sh` restores them. The backup bucket is set in
the console under **Settings > Backup storage**. Setup, the restore drill, and failure handling are
in `docs/runbook.md`.
