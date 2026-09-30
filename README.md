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
signs the session cookie and encrypts TOTP secrets, so changing it signs everyone out and makes
stored TOTP secrets unreadable (recreate operators).

## Load places

```sh
scripts/load_places.sh    # 1 country, 37 states, 774 LGAs and place aliases; safe to re-run
```

Boundaries are geoBoundaries gbOpen for Nigeria (CC BY 4.0, attribution: William & Mary geoLab),
pinned by SHA-256 in `src/africasignal/places/load.py`. Extra names live in
`config/place_aliases.yaml`. Pass `--cities NG.txt` to add GeoNames cities of 50,000 or more.

## Configuration

Set through environment variables (see `src/africasignal/config.py`). With `ENV=staging` or
`ENV=production`, startup fails with `RuntimeError` if `DATABASE_URL`, `S3_*`, `ANTHROPIC_API_KEY`,
`EMAIL_*` or `SECRET_KEY` is missing.
