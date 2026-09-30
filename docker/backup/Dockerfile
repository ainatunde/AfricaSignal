# Backup job image: pg_dump 16 (matches the postgis/postgis:16 server) plus rclone, curl and openssl.
FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends rclone curl openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/backup.sh scripts/restore.sh scripts/backup-daemon.sh /opt/africasignal/scripts/
COPY scripts/lib /opt/africasignal/scripts/lib

USER postgres
ENTRYPOINT ["/opt/africasignal/scripts/backup-daemon.sh"]
