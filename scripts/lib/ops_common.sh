# Shared helpers for scripts/backup.sh, restore.sh and restore-drill.sh. Source this file; do not run it.
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
  OPS_LAST_ERROR="$*"
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
  if [ -n "$2" ]; then
    export "RCLONE_CONFIG_${name}_PROVIDER=Other"
    export "RCLONE_CONFIG_${name}_ENDPOINT=$2"
  else
    export "RCLONE_CONFIG_${name}_PROVIDER=AWS" # no endpoint: plain AWS S3
  fi
  export "RCLONE_CONFIG_${name}_ACCESS_KEY_ID=$3"
  export "RCLONE_CONFIG_${name}_SECRET_ACCESS_KEY=$4"
  # Scoped tokens (Cloudflare R2, Backblaze B2) usually cannot create buckets; never try to.
  export "RCLONE_CONFIG_${name}_NO_CHECK_BUCKET=true"
}

# ---- settings the operator manages in the console -----------------------------------------------
# Backup and storage settings are saved in the operator console (Settings page) and read here with
# `python -m africasignal.admin get-setting KEY --reveal`. That already falls back to the environment
# variable of the same name in capitals and then to the default, so the console wins over `.env`.
# When the console cannot be read (Python or the app is not installed, the database is down, which
# is the case in a disaster recovery), the plain environment variable is used instead and a warning
# is logged. Set OPS_USE_CONSOLE_SETTINGS=0 to skip the console entirely.

# Find a Python that can import africasignal; sets OPS_CONSOLE (1 usable, 0 not) once per run.
console_settings_init() {
  [ -z "${OPS_CONSOLE:-}" ] || return 0
  OPS_CONSOLE=0
  if [ "${OPS_USE_CONSOLE_SETTINGS:-1}" = 0 ] || [ -z "${DATABASE_URL:-}" ]; then
    return 0
  fi
  local py
  for py in "${OPS_PYTHON:-}" python3 python; do
    [ -n "$py" ] || continue
    if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import africasignal.admin' >/dev/null 2>&1; then
      OPS_PYTHON="$py"
      OPS_CONSOLE=1
      return 0
    fi
  done
  log "warning: africasignal is not importable here; using environment variables, not console settings"
}

# setting_value KEY ENV_NAME: print the console value of KEY, else the environment variable ENV_NAME.
setting_value() {
  local key="$1" envname="$2" value rc=0
  console_settings_init
  if [ "$OPS_CONSOLE" = 1 ]; then
    value="$("$OPS_PYTHON" -m africasignal.admin get-setting "$key" --reveal 2>"$WORKDIR/setting.err")" || rc=$?
    case "$rc" in
      0)
        printf '%s' "$value"
        return 0
        ;;
      2) ;; # not set in the console, the environment or as a default
      *)
        log "warning: console settings could not be read ($(tail -n 1 "$WORKDIR/setting.err" | cut -c1-160)); using environment variables"
        OPS_CONSOLE=0
        ;;
    esac
  fi
  printf '%s' "${!envname:-}"
}

# Fill BACKUP_S3_* and BACKUP_RETAIN_DAYS from the console (or the environment).
load_backup_settings() {
  BACKUP_RETAIN_DAYS="$(setting_value backup_retain_days BACKUP_RETAIN_DAYS)"
  [ -z "${BACKUP_DIR:-}" ] || return 0
  BACKUP_S3_ENDPOINT_URL="$(setting_value backup_s3_endpoint_url BACKUP_S3_ENDPOINT_URL)"
  BACKUP_S3_BUCKET="$(setting_value backup_s3_bucket BACKUP_S3_BUCKET)"
  BACKUP_S3_ACCESS_KEY_ID="$(setting_value backup_s3_access_key_id BACKUP_S3_ACCESS_KEY_ID)"
  BACKUP_S3_SECRET_ACCESS_KEY="$(setting_value backup_s3_secret_access_key BACKUP_S3_SECRET_ACCESS_KEY)"
  BACKUP_S3_PREFIX="$(setting_value backup_s3_prefix BACKUP_S3_PREFIX)"
}

# Fill S3_* (the app's evidence bucket) from the console (or the environment).
load_app_bucket_settings() {
  S3_ENDPOINT_URL="$(setting_value s3_endpoint_url S3_ENDPOINT_URL)"
  S3_BUCKET="$(setting_value s3_bucket S3_BUCKET)"
  S3_ACCESS_KEY_ID="$(setting_value s3_access_key_id S3_ACCESS_KEY_ID)"
  S3_SECRET_ACCESS_KEY="$(setting_value s3_secret_access_key S3_SECRET_ACCESS_KEY)"
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

# ---- status the app reads to raise alerts -------------------------------------------------------
# backup.sh and restore-drill.sh leave a small JSON record in the `setting` table of the app's
# database, under `ops.backup_status` and `ops.restore_drill_status`. The scheduler's
# `check_backups` job (src/africasignal/backup_alerts.py) reads them and alerts when the last
# successful backup is too old or the last drill failed. Recording is best effort: it never fails
# the script that calls it (a failed backup must still exit with its own error), and it is skipped
# without a word when there is no database or no `setting` table yet.

# json_str TEXT: TEXT as a JSON string literal, control characters dropped, cut to 300 characters.
json_str() {
  local text
  text="$(printf '%s' "$1" | tr -d '\000-\037' | cut -c1-300 | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
  printf '"%s"' "$text"
}

# record_ops_status KEY JSON_OBJECT: merge JSON_OBJECT into setting row KEY. Uses
# OPS_STATUS_DATABASE_URL, else DATABASE_URL. Set OPS_RECORD_STATUS=0 to turn it off.
record_ops_status() {
  local key="$1" patch="$2" url err
  [ "${OPS_RECORD_STATUS:-1}" != 0 ] || return 0
  url="${OPS_STATUS_DATABASE_URL:-${DATABASE_URL:-}}"
  [ -n "$url" ] && command -v psql >/dev/null 2>&1 || return 0
  url="$(libpq_url "$url")"
  [ "$(psql "$url" -Atq -c "SELECT to_regclass('public.setting') IS NOT NULL" 2>/dev/null || true)" = t ] || return 0
  if ! err="$(psql "$url" -v ON_ERROR_STOP=1 -Atq -v key="$key" -v patch="$patch" 2>&1 >/dev/null <<'SQL'
INSERT INTO setting (key, value) VALUES (:'key', :'patch'::jsonb)
ON CONFLICT (key) DO UPDATE SET value = setting.value || EXCLUDED.value, updated_at = now();
SQL
  )"; then
    log "warning: could not record $key: $(printf '%s' "$err" | tail -n 1 | cut -c1-160)"
  fi
  return 0
}
