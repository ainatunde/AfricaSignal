#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh into a database you name explicitly.
#
# Usage:
#   scripts/restore.sh --list
#   scripts/restore.sh --target-url URL [--backup latest|NAME | --file PATH] [--recreate]
#                      [--restore-objects] [--verify-objects N]
#
#   --list              list the dumps in the backup store and exit
#   --target-url URL    database to restore into (or set RESTORE_DATABASE_URL). Required; there is no default
#   --backup NAME       dump to restore, as shown by --list (default: latest)
#   --file PATH         restore a local dump file instead of downloading one
#   --recreate          drop and create the target database first (otherwise it must be empty)
#   --restore-objects   copy the backed-up objects into the RESTORE_S3_* bucket
#   --verify-objects N  check that N random evidence_document.storage_key objects exist in the RESTORE_S3_* bucket
#   --overwrite-live    allow the target to be the same database as DATABASE_URL, or ENV=production
#
# Environment: the BACKUP_* variables described in scripts/backup.sh, plus BACKUP_PASSPHRASE_FILE
# when dumps are encrypted, and RESTORE_S3_ENDPOINT_URL, RESTORE_S3_BUCKET, RESTORE_S3_ACCESS_KEY_ID,
# RESTORE_S3_SECRET_ACCESS_KEY for the bucket that receives objects. Objects are never restored
# to the app's own bucket unless RESTORE_S3_* names it.
set -Eeuo pipefail

OPS_TAG=restore
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/ops_common.sh
. "$here/lib/ops_common.sh"
OPS_LOG_FILE="${RESTORE_LOG_FILE:-}"

target_url="${RESTORE_DATABASE_URL:-}"
backup_name=latest
local_file=""
do_list=0 recreate=0 restore_objects=0 overwrite_live=0
verify_objects=0

while [ $# -gt 0 ]; do
  case "$1" in
    --list) do_list=1 ;;
    --target-url) target_url="${2:?--target-url needs a value}"; shift ;;
    --backup) backup_name="${2:?--backup needs a value}"; shift ;;
    --file) local_file="${2:?--file needs a value}"; shift ;;
    --recreate) recreate=1 ;;
    --restore-objects) restore_objects=1 ;;
    --verify-objects) verify_objects="${2:?--verify-objects needs a value}"; shift ;;
    --overwrite-live) overwrite_live=1 ;;
    -h | --help)
      sed -n '2,/^set -Eeuo/p' "$0" | sed '$d; s/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

WORKDIR=""
trap '[ -z "$WORKDIR" ] || rm -rf "$WORKDIR"' EXIT
umask 077
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/africasignal-restore.XXXXXX")"
passphrase_init

case "$verify_objects" in '' | *[!0-9]*) die "--verify-objects needs a number" ;; esac

if [ -z "$local_file" ] || [ "$do_list" -eq 1 ]; then
  rclone_init
  backup_root_init
fi

if [ "$do_list" -eq 1 ]; then
  rclone lsl "$BACKUP_ROOT/db" --filter '- *.sha256' --filter '+ africasignal-*.dump*' --filter '- *' | sort -k4
  exit 0
fi

[ -n "$target_url" ] || die "no target: pass --target-url (or RESTORE_DATABASE_URL); there is deliberately no default"
require_cmd psql pg_restore sha256sum
target="$(libpq_url "$target_url")"
dbname="$(url_dbname "$target")"
case "$dbname" in '' | *[!A-Za-z0-9_]*) die "target database name must be letters, digits and underscores, got '$dbname'" ;; esac

# Guard rails: a restore replaces data, and the obvious mistake is running it against the live database.
if [ -n "${DATABASE_URL:-}" ] && [ "$(libpq_url "$DATABASE_URL")" = "$target" ] && [ "$overwrite_live" -ne 1 ]; then
  die "target is the same database as DATABASE_URL; pass --overwrite-live if you really mean it"
fi
if [ "${ENV:-development}" = "production" ] && [ "$overwrite_live" -ne 1 ]; then
  die "ENV=production: restoring here is only for disaster recovery; pass --overwrite-live to confirm"
fi

# ---- fetch and verify the dump -------------------------------------------------------------
if [ -n "$local_file" ]; then
  [ -f "$local_file" ] || die "no such file: $local_file"
  name="$(basename "$local_file")"
  cp "$local_file" "$WORKDIR/$name"
  if [ -f "$local_file.sha256" ]; then cp "$local_file.sha256" "$WORKDIR/$name.sha256"; fi
else
  if [ "$backup_name" = latest ]; then
    backup_name="$(rclone lsf --files-only "$BACKUP_ROOT/db" | grep -E '^africasignal-.*\.dump(\.enc)?$' | sort | tail -n 1 || true)"
    [ -n "$backup_name" ] || die "no backups found in $BACKUP_ROOT/db"
  fi
  name="$backup_name"
  log "downloading $name"
  rclone copyto "$BACKUP_ROOT/db/$name" "$WORKDIR/$name"
  rclone copyto "$BACKUP_ROOT/db/$name.sha256" "$WORKDIR/$name.sha256" 2>/dev/null || true
fi

if [ -f "$WORKDIR/$name.sha256" ]; then
  expected="$(cut -d' ' -f1 "$WORKDIR/$name.sha256")"
  actual="$(sha256sum "$WORKDIR/$name" | cut -d' ' -f1)"
  [ "$expected" = "$actual" ] || die "checksum mismatch for $name (expected $expected, got $actual)"
  log "checksum ok"
else
  log "warning: no .sha256 alongside $name; skipping checksum"
fi

dump="$WORKDIR/$name"
case "$name" in
  *.enc)
    require_cmd openssl
    [ -n "${BACKUP_PASSPHRASE_FILE:-}" ] && [ -s "$BACKUP_PASSPHRASE_FILE" ] || die "$name is encrypted: set BACKUP_PASSPHRASE_FILE"
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass "file:$BACKUP_PASSPHRASE_FILE" -in "$dump" -out "${dump%.enc}" ||
      die "decryption failed (wrong passphrase or corrupt file)"
    dump="${dump%.enc}"
    ;;
esac
pg_restore --list "$dump" >/dev/null || die "$name is not a readable pg_dump archive"

# ---- prepare the target --------------------------------------------------------------------
if [ "$recreate" -eq 1 ]; then
  maint="$(url_with_maintenance_db "$target")"
  log "recreating database $dbname"
  psql "$maint" -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS \"$dbname\" WITH (FORCE)" -c "CREATE DATABASE \"$dbname\""
fi

# The target must be empty: tables that belong to an extension (PostGIS's spatial_ref_sys) do not count.
existing="$(psql "$target" -v ON_ERROR_STOP=1 -Atq -c "
  SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'
    AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')")"
[ "$existing" = 0 ] || die "target database $dbname already has $existing table(s); use --recreate or an empty database"

# ---- restore -------------------------------------------------------------------------------
log "restoring $name into $dbname"
pg_restore --no-owner --no-acl --exit-on-error --single-transaction --dbname="$target" "$dump"
psql "$target" -v ON_ERROR_STOP=1 -q -c "ANALYZE"

# ---- checks --------------------------------------------------------------------------------
version="$(psql "$target" -v ON_ERROR_STOP=1 -Atq -c "SELECT version_num FROM alembic_version")" ||
  die "restored database has no alembic_version table"
[ -n "$version" ] || die "alembic_version is empty after restore"
log "schema version: $version"
for t in source evidence_document measurement assessment_version app_user job; do
  n="$(psql "$target" -Atq -c "SELECT count(*) FROM \"$t\"" 2>/dev/null || echo 'missing')"
  log "rows in $t: $n"
  [ "$n" != missing ] || die "table $t is missing after restore"
done

if [ "$restore_objects" -eq 1 ] || [ "$verify_objects" -gt 0 ]; then
  [ -n "${RESTORE_S3_BUCKET:-}" ] || die "set RESTORE_S3_BUCKET (and RESTORE_S3_* credentials) for object restore or verification"
  rclone_init
  rclone_define_s3 restore "${RESTORE_S3_ENDPOINT_URL:-}" "${RESTORE_S3_ACCESS_KEY_ID:-}" "${RESTORE_S3_SECRET_ACCESS_KEY:-}"
  if [ "$restore_objects" -eq 1 ]; then
    [ -n "${BACKUP_ROOT:-}" ] || backup_root_init
    log "copying objects into $RESTORE_S3_BUCKET"
    rclone copy "$BACKUP_ROOT/objects" "restore:$RESTORE_S3_BUCKET" --size-only --transfers 8 --checkers 16
  fi
  if [ "$verify_objects" -gt 0 ]; then
    missing=0 checked=0
    while IFS= read -r key; do
      [ -n "$key" ] || continue
      checked=$((checked + 1))
      if [ -z "$(rclone lsf --files-only "restore:$RESTORE_S3_BUCKET/$key" 2>/dev/null)" ]; then
        log "MISSING object: $key"
        missing=$((missing + 1))
      fi
    done < <(psql "$target" -Atq -c "SELECT storage_key FROM evidence_document ORDER BY random() LIMIT $verify_objects")
    [ "$missing" -eq 0 ] || die "$missing of $checked sampled evidence objects are missing from $RESTORE_S3_BUCKET"
    log "sampled $checked evidence object(s): all present"
  fi
fi

log "restore ok: $name -> $dbname (schema $version)"
