#!/usr/bin/env bash
# Nightly backup: a custom-format pg_dump plus object copies in a separate, private bucket (or a
# local directory), then old dumps are pruned. Evidence and deletion-ledger prefixes mirror the
# current app bucket so expired evidence and retired deletion fingerprints age out there too.
#
# Usage: scripts/backup.sh [--no-objects]
#
# Settings: the backup bucket, the app's evidence bucket and the retention are read from the operator
# console (Settings page; needs DATABASE_URL and SECRET_KEY, and africasignal installed), and the
# environment variable of the same name is used when the console has nothing or cannot be read.
# Console key -> variable: backup_s3_endpoint_url, backup_s3_bucket, backup_s3_access_key_id,
# backup_s3_secret_access_key, backup_s3_prefix, backup_retain_days -> BACKUP_S3_ENDPOINT_URL, ...;
# s3_endpoint_url, s3_bucket, s3_access_key_id, s3_secret_access_key -> S3_*.
#
# Environment:
#   DATABASE_URL                     database to dump (postgresql:// or postgresql+psycopg://)
#   SECRET_KEY                       needed to read secrets saved in the console
#   OPS_USE_CONSOLE_SETTINGS=0       ignore the console and use only environment variables
#   BACKUP_S3_ENDPOINT_URL           S3 endpoint of the backup bucket (Cloudflare R2, Backblaze B2, MinIO)
#   BACKUP_S3_BUCKET                 backup bucket; must not be the app's own bucket
#   BACKUP_S3_ACCESS_KEY_ID / BACKUP_S3_SECRET_ACCESS_KEY   credentials for the backup bucket only
#   BACKUP_S3_PREFIX                 key prefix (default africasignal/$ENV)
#   BACKUP_DIR                       use a local directory instead of S3 (development, tests)
#   BACKUP_RETAIN_DAYS               keep dumps this many days (default 30)
#   BACKUP_PASSPHRASE_FILE           encrypt dumps with AES-256 using the passphrase in this file.
#                                    Required unless ENV is development (or unset)
#   BACKUP_PASSPHRASE                the same, with the passphrase given directly (less safe than a file)
#   BACKUP_HEARTBEAT_URL             healthchecks.io-style URL: pinged on start (/start), success, and failure (/fail)
#   BACKUP_LOG_FILE                  also append log lines here
#   OPS_RECORD_STATUS=0              do not leave the last success/failure in the app database
#                                    (setting ops.backup_status, read by the check_backups alert job)
#   S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY   the app's bucket, copied unless --no-objects
#
# Exit status is non-zero on any failure, and the failure heartbeat is sent, so a missed or
# failed night is noticed by whatever watches the heartbeat.
set -Eeuo pipefail

OPS_TAG=backup
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/ops_common.sh
. "$here/lib/ops_common.sh"
OPS_LOG_FILE="${BACKUP_LOG_FILE:-}"

copy_objects=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-objects) copy_objects=0 ;;
    -h | --help)
      sed -n '2,/^set -Eeuo/p' "$0" | sed '$d; s/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

ping_heartbeat() {
  [ -n "${BACKUP_HEARTBEAT_URL:-}" ] || return 0
  curl -fsS -m 15 --retry 3 -o /dev/null "${BACKUP_HEARTBEAT_URL%/}$1" || log "warning: heartbeat ping failed"
}

WORKDIR=""
on_exit() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    log "backup FAILED (exit $status)"
    ping_heartbeat /fail
    record_ops_status ops.backup_status "{\"last_failure_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"last_failure_reason\":$(json_str "${OPS_LAST_ERROR:-exit status $status}")}"
  fi
  [ -z "$WORKDIR" ] || rm -rf "$WORKDIR"
  exit "$status"
}
trap on_exit EXIT

[ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL is not set"
require_cmd pg_dump pg_restore sha256sum

umask 077
WORKDIR="$(mktemp -d "${BACKUP_WORKDIR:-${TMPDIR:-/tmp}}/africasignal-backup.XXXXXX")"
pgpass_init # the database password goes in a private file, not on the pg_dump command line
load_backup_settings
retain_days="${BACKUP_RETAIN_DAYS:-30}"
case "$retain_days" in '' | *[!0-9]* | 0) die "backup_retain_days / BACKUP_RETAIN_DAYS must be a positive integer" ;; esac
passphrase_init
# Dumps hold readers' email addresses and the rest of the database. Outside development they are
# only ever stored encrypted (security review S-15).
if [ "${ENV:-development}" != development ] && [ -z "${BACKUP_PASSPHRASE_FILE:-}" ]; then
  die "ENV=${ENV}: dumps must be encrypted; set BACKUP_PASSPHRASE_FILE (or BACKUP_PASSPHRASE)"
fi
if [ -n "${BACKUP_PASSPHRASE_FILE:-}" ]; then
  require_cmd openssl
  [ -s "$BACKUP_PASSPHRASE_FILE" ] || die "BACKUP_PASSPHRASE_FILE is missing or empty"
fi
rclone_init
backup_root_init

db_url="$(libpq_url "$DATABASE_URL")"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="africasignal-${stamp}.dump"
dump="$WORKDIR/$name"

ping_heartbeat /start
log "dumping $(url_dbname "$db_url") to $name"
pg_dump --format=custom --no-owner --no-acl --dbname="$db_url" --file="$dump"

# A dump that pg_restore cannot even list is worthless; find out now, not during a disaster.
entries="$(pg_restore --list "$dump" | grep -vc '^;' || true)"
[ "${entries:-0}" -gt 0 ] || die "dump has no entries"
log "dump readable: $entries entries, $(wc -c <"$dump") bytes"

upload="$dump"
if [ -n "${BACKUP_PASSPHRASE_FILE:-}" ]; then
  openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt -pass "file:$BACKUP_PASSPHRASE_FILE" -in "$dump" -out "$dump.enc"
  rm -f "$dump"
  name="$name.enc"
  upload="$dump.enc"
  log "encrypted dump"
fi

sum="$(sha256sum "$upload" | cut -d' ' -f1)"
printf '%s  %s\n' "$sum" "$name" >"$WORKDIR/$name.sha256"
size="$(wc -c <"$upload" | tr -d ' ')"

rclone copyto "$upload" "$BACKUP_ROOT/db/$name"
rclone copyto "$WORKDIR/$name.sha256" "$BACKUP_ROOT/db/$name.sha256"
remote_size="$(rclone lsf --format s --files-only "$BACKUP_ROOT/db/$name" | tr -d ' \r\n')"
[ "$remote_size" = "$size" ] || die "uploaded size $remote_size does not match local size $size"
log "uploaded db/$name ($size bytes, sha256 $sum)"

[ "$copy_objects" -eq 0 ] || load_app_bucket_settings
if [ "$copy_objects" -eq 1 ] && [ -n "${S3_BUCKET:-}" ]; then
  if [ -z "${BACKUP_DIR:-}" ] && [ "${S3_ENDPOINT_URL:-}" = "${BACKUP_S3_ENDPOINT_URL:-}" ] && [ "$S3_BUCKET" = "$BACKUP_S3_BUCKET" ]; then
    die "the backup bucket is the app's own bucket; use a separate bucket (ideally another account)"
  fi
  rclone_define_s3 app "${S3_ENDPOINT_URL:-}" "${S3_ACCESS_KEY_ID:-}" "${S3_SECRET_ACCESS_KEY:-}"
  # Keep non-retention-managed objects such as NBS uploads immutable in the backup copy.
  # Evidence and the deletion ledger are current-state mirrors: retention and ledger pruning in
  # the app bucket must also take effect in backups. A restore from an older database dump remains
  # quarantined until current retention and deletion replay have run.
  log "copying non-expiring app objects from $S3_BUCKET"
  rclone copy "app:$S3_BUCKET" "$BACKUP_ROOT/objects" \
    --exclude "/evidence/**" --exclude "/deletions/**" \
    --immutable --transfers 8 --checkers 16
  for prefix in evidence deletions; do
    log "mirroring current $prefix/ objects into objects/$prefix/"
    rclone sync "app:$S3_BUCKET/$prefix" "$BACKUP_ROOT/objects/$prefix" \
      --checksum --transfers 8 --checkers 16
    rclone check "app:$S3_BUCKET/$prefix" "$BACKUP_ROOT/objects/$prefix" \
      --one-way --download
  done
elif [ "$copy_objects" -eq 1 ]; then
  log "warning: S3_BUCKET not set, skipping object copy"
fi

# Prune only after the new dump is safely uploaded, so a run of failures never empties the store.
log "pruning dumps older than $retain_days days"
rclone delete "$BACKUP_ROOT/db" --min-age "${retain_days}d" --filter '+ africasignal-*' --filter '- *'

kept="$(rclone lsf --files-only "$BACKUP_ROOT/db" | grep -cE '\.(dump|dump\.enc)$' || true)"
log "backup ok: $name, $kept dump(s) kept"
record_ops_status ops.backup_status "{\"last_success_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"last_success_name\":$(json_str "$name"),\"last_success_bytes\":$size}"
ping_heartbeat ""
