# Backup job image: the app (so the scripts can read the settings saved in the operator console),
# pg_dump 16 from the PostgreSQL apt repository (matches the postgis/postgis:16 server), rclone,
# curl and openssl.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl openssl rclone lsb-release postgresql-common \
    && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md requirements.lock ./
COPY src ./src
RUN pip install --no-cache-dir pip==26.2.1 setuptools==84.0.0 wheel==0.48.0 \
    && pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps --no-build-isolation . \
    && pip check

COPY scripts/backup.sh scripts/restore.sh scripts/restore-drill.sh scripts/backup-daemon.sh /opt/africasignal/scripts/
COPY scripts/lib /opt/africasignal/scripts/lib

RUN useradd --create-home app
USER app
ENTRYPOINT ["/opt/africasignal/scripts/backup-daemon.sh"]
