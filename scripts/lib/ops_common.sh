# Shared helpers for scripts/backup.sh and scripts/restore.sh. Source this file; do not run it.
# shellcheck shell=bash

log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) [${OPS_TAG:-ops}] $*"
  printf '%s\n' "$line" >&2
  if [ -n "${OPS_LOG_FILE:-}" ]; then
    printf '%s\n' "$line" >>"$OPS_LOG_FILE" 2>/dev/null || true
  fi
}

die() {
  log "ERROR: $*"
  exit 1
}

require_cmd() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "required command not found: $c"
  done
}

# The app's DATABASE_URL uses the SQLAlchemy driver suffix (postgresql+psycopg://); libpq tools
# want a plain postgresql:// URL.
libpq_url() {
  local url="$1"
  printf '%s' "$url" | sed -E 's#^postgres(ql)?\+[a-z0-9_]+://#postgresql://#; s#^postgres://#postgresql://#'
}

# Database name of a libpq URL (path component, without query string).
url_dbname() {
  printf '%s' "$1" | sed -E 's#^[^:]+://[^/]*/([^/?]*).*$#\1#'
}

# The same URL pointed at the maintenance database `postgres`.
url_with_maintenance_db() {
  printf '%s' "$1" | sed -E 's#^([^:]+://[^/]*/)[^/?]*#\1postgres#'
}

# Point rclone at an empty config so credentials come only from RCLONE_CONFIG_* variables.
rclone_init() {
  require_cmd rclone
  if [ -z "${RCLONE_CONFIG:-}" ]; then
    RCLONE_CONFIG="$WORKDIR/rclone.conf"
    : >"$RCLONE_CONFIG"
    export RCLONE_CONFIG
  fi
}

# Define an rclone S3 remote named $1 from endpoint $2, access key $3, secret $4.
rclone_define_s3() {
  local name
  name="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
  export "RCLONE_CONFIG_${name}_TYPE=s3"
  export "RCLONE_CONFIG_${name}_PROVIDER=Other"
  export "RCLONE_CONFIG_${name}_ENDPOINT=$2"
  export "RCLONE_CONFIG_${name}_ACCESS_KEY_ID=$3"
  export "RCLONE_CONFIG_${name}_SECRET_ACCESS_KEY=$4"
  # Scoped tokens (Cloudflare R2, Backblaze B2) usually cannot create buckets; never try to.
  export "RCLONE_CONFIG_${name}_NO_CHECK_BUCKET=true"
}

# Sets BACKUP_ROOT to the rclone path under which dumps (db/) and object copies (objects/) live:
# a local directory when BACKUP_DIR is set, otherwise the BACKUP_S3_* bucket under a prefix.
backup_root_init() {
  if [ -n "${BACKUP_DIR:-}" ]; then
    mkdir -p "$BACKUP_DIR"
    BACKUP_ROOT="$BACKUP_DIR"
    return
  fi
  [ -n "${BACKUP_S3_BUCKET:-}" ] || die "set BACKUP_S3_BUCKET (or BACKUP_DIR for a local directory)"
  [ -n "${BACKUP_S3_ACCESS_KEY_ID:-}" ] && [ -n "${BACKUP_S3_SECRET_ACCESS_KEY:-}" ] ||
    die "set BACKUP_S3_ACCESS_KEY_ID and BACKUP_S3_SECRET_ACCESS_KEY"
  rclone_define_s3 backup "${BACKUP_S3_ENDPOINT_URL:-}" "$BACKUP_S3_ACCESS_KEY_ID" "$BACKUP_S3_SECRET_ACCESS_KEY"
  local prefix="${BACKUP_S3_PREFIX:-africasignal/${ENV:-development}}"
  prefix="${prefix#/}"
  prefix="${prefix%/}"
  BACKUP_ROOT="backup:${BACKUP_S3_BUCKET}/${prefix}"
}

# Sets BACKUP_PASSPHRASE_FILE from BACKUP_PASSPHRASE when only the latter is given (needs $WORKDIR).
passphrase_init() {
  if [ -z "${BACKUP_PASSPHRASE_FILE:-}" ] && [ -n "${BACKUP_PASSPHRASE:-}" ]; then
    BACKUP_PASSPHRASE_FILE="$WORKDIR/passphrase"
    printf '%s\n' "$BACKUP_PASSPHRASE" >"$BACKUP_PASSPHRASE_FILE"
  fi
}
