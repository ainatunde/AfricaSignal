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
