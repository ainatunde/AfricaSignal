#!/usr/bin/env bash
# Nightly backup: a custom-format pg_dump of the database plus a copy of the evidence bucket,
# both sent to a separate, private bucket (or a local directory), then old dumps are pruned.
#
# Usage: scripts/backup.sh [--no-objects]
#
# Environment:
#   DATABASE_URL                     database to dump (postgresql:// or postgresql+psycopg://)
#   BACKUP_S3_ENDPOINT_URL           S3 endpoint of the backup bucket (Cloudflare R2, Backblaze B2, MinIO)
#   BACKUP_S3_BUCKET                 backup bucket; must not be the app's own bucket
#   BACKUP_S3_ACCESS_KEY_ID / BACKUP_S3_SECRET_ACCESS_KEY   credentials for the backup bucket only
#   BACKUP_S3_PREFIX                 key prefix (default africasignal/$ENV)
#   BACKUP_DIR                       use a local directory instead of S3 (development, tests)
#   BACKUP_RETAIN_DAYS               keep dumps this many days (default 30)
#   BACKUP_PASSPHRASE_FILE           encrypt dumps with AES-256 using the passphrase in this file
#   BACKUP_PASSPHRASE                the same, with the passphrase given directly (less safe than a file)
#   BACKUP_HEARTBEAT_URL             healthchecks.io-style URL: pinged on start (/start), success, and failure (/fail)
#   BACKUP_LOG_FILE                  also append log lines here
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
  fi
  [ -z "$WORKDIR" ] || rm -rf "$WORKDIR"
  exit "$status"
}
trap on_exit EXIT

[ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL is not set"
retain_days="${BACKUP_RETAIN_DAYS:-30}"
case "$retain_days" in '' | *[!0-9]* | 0) die "BACKUP_RETAIN_DAYS must be a positive integer" ;; esac
require_cmd pg_dump pg_restore sha256sum

umask 077
WORKDIR="$(mktemp -d "${BACKUP_WORKDIR:-${TMPDIR:-/tmp}}/africasignal-backup.XXXXXX")"
passphrase_init
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

if [ "$copy_objects" -eq 1 ] && [ -n "${S3_BUCKET:-}" ]; then
  if [ -z "${BACKUP_DIR:-}" ] && [ "${S3_ENDPOINT_URL:-}" = "${BACKUP_S3_ENDPOINT_URL:-}" ] && [ "$S3_BUCKET" = "$BACKUP_S3_BUCKET" ]; then
    die "the backup bucket is the app's own bucket; use a separate bucket (ideally another account)"
  fi
  rclone_define_s3 app "${S3_ENDPOINT_URL:-}" "${S3_ACCESS_KEY_ID:-}" "${S3_SECRET_ACCESS_KEY:-}"
  # Evidence objects are content-addressed and never change: copy (never sync, so deleting an
  # object in the app bucket cannot delete its backup) and fail loudly if one changes size.
  log "copying app bucket $S3_BUCKET to objects/"
  rclone copy "app:$S3_BUCKET" "$BACKUP_ROOT/objects" --size-only --immutable --transfers 8 --checkers 16
elif [ "$copy_objects" -eq 1 ]; then
  log "warning: S3_BUCKET not set, skipping object copy"
fi

# Prune only after the new dump is safely uploaded, so a run of failures never empties the store.
log "pruning dumps older than $retain_days days"
rclone delete "$BACKUP_ROOT/db" --min-age "${retain_days}d" --filter '+ africasignal-*' --filter '- *'

kept="$(rclone lsf --files-only "$BACKUP_ROOT/db" | grep -cE '\.(dump|dump\.enc)$' || true)"
log "backup ok: $name, $kept dump(s) kept"
ping_heartbeat ""
