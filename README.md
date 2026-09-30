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

## Configuration

Set through environment variables (see `src/africasignal/config.py`). With `ENV=staging` or
`ENV=production`, startup fails with `RuntimeError` if `DATABASE_URL`, `S3_*`, `ANTHROPIC_API_KEY`,
`EMAIL_*` or `SECRET_KEY` is missing.
