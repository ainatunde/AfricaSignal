#!/usr/bin/env bash
# Restore drill: prove that a backup can really be restored, and fail loudly when it cannot.
#
# Usage:
#   scripts/restore-drill.sh [--source-url URL] [--latest] [--verify-objects N] [--keep] [--no-record]
#
# Default ("fresh") mode takes a new backup of the source database with scripts/backup.sh, restores
# it with scripts/restore.sh into a scratch database on the same server, and compares the two:
#   - the same tables, with row counts that match (a source that is being written to during the drill
#     is allowed to move inside the range seen before and after the dump, not outside it),
#   - the same alembic version and the same PostgreSQL extensions, and PostGIS working,
#   - the same content (an md5 of every row) in the key tables, when the source did not change while
#     the dump ran,
#   - every id sequence at or past its table's highest id, so the restored database accepts inserts.
# Any difference exits non-zero with the reason. Nothing needs a staging host: a PostgreSQL with
# PostGIS and a BACKUP_DIR are enough, so this runs in CI and on a laptop (see docs/runbook.md 4.3).
#
# --latest restores the newest backup already in the store instead of taking a new one, and checks
# that it restores, is at a schema version, has every table the source has (when the versions match)
# and that the key tables are not empty when the source's are. Use it on a staging host that has
# access to the real backup bucket. Row counts are only reported, because the backup is hours old.
#
#   --source-url URL    database being backed up (default: DATABASE_URL). The scratch database is
#                       created next to it on the same server, so the role needs CREATEDB
#   --latest            restore the newest existing backup instead of taking a new one
#   --verify-objects N  also check that N random evidence objects listed in the restored database exist
#                       in the RESTORE_S3_* bucket (see scripts/restore.sh)
#   --keep              keep the scratch database afterwards (it is dropped by default)
#   --no-record         do not leave the result in the source database (setting
#                       ops.restore_drill_status, which the check_backups job alerts on)
#
# Where the backups go comes from the console settings or the BACKUP_* variables, exactly as in
# scripts/backup.sh; BACKUP_DIR=/some/dir is enough for a local drill. DRILL_DIGEST_TABLES overrides
# the tables whose content is compared (default: source evidence_document measurement
# assessment_version place).
#
# The drill never touches the source database except to read it and to record its result. The
# scratch database is named <source>_drill_<UTC timestamp>, always different from the source, so
# restore.sh is told --overwrite-live (its guard exists for a person typing a target by hand).
set -Eeuo pipefail

OPS_TAG=drill
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/ops_common.sh
. "$here/lib/ops_common.sh"
OPS_LOG_FILE="${DRILL_LOG_FILE:-}"

source_url="${DATABASE_URL:-}"
mode=fresh
verify_objects=0
keep=0
record=1
while [ $# -gt 0 ]; do
  case "$1" in
    --source-url) source_url="${2:?--source-url needs a value}"; shift ;;
    --latest) mode=latest ;;
    --verify-objects) verify_objects="${2:?--verify-objects needs a value}"; shift ;;
    --keep) keep=1 ;;
    --no-record) record=0 ;;
    -h | --help)
      sed -n '2,/^set -Eeuo/p' "$0" | sed '$d; s/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# last_error LOGFILE: the message of the last "ERROR:" line a called script logged, else the last
# line it wrote (a psql or pg_restore error that the script did not catch itself).
last_error() {
  local msg
  msg="$(sed -n 's/^[^ ]* \[[a-z]*\] ERROR: //p' "$1" | tail -n 1)"
  [ -n "$msg" ] || msg="$(grep -v '^[[:space:]]*$' "$1" | tail -n 1 | sed -E 's/^[^ ]+ \[[a-z]+\] //')"
  printf '%s' "$msg"
}

WORKDIR=""
scratch_url=""
scratch_db=""
maint_url=""
backup_used=""
restore_seconds=0
compared=0
started="$(date +%s)"

on_exit() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    log "restore drill FAILED (exit $status)"
  fi
  if [ -n "$scratch_db" ] && [ "$keep" -eq 0 ]; then
    psql "$maint_url" -q -c "DROP DATABASE IF EXISTS \"$scratch_db\" WITH (FORCE)" >/dev/null 2>&1 ||
      log "warning: could not drop scratch database $scratch_db"
  elif [ -n "$scratch_db" ]; then
    log "kept scratch database $scratch_db"
  fi
  if [ "$record" -eq 1 ] && [ -n "$source_url" ]; then
    local finished ok
    finished="$(date +%s)"
    if [ "$status" -eq 0 ]; then ok=true; else ok=false; fi
    local patch
    patch="{\"last_run_at\":\"$(now_utc)\",\"ok\":$ok,\"mode\":\"$mode\",\"backup\":$(json_str "$backup_used"),\"restore_seconds\":$restore_seconds,\"duration_seconds\":$((finished - started)),\"tables_compared\":$compared,\"detail\":$(json_str "${OPS_LAST_ERROR:-}")"
    if [ "$ok" = true ]; then patch="$patch,\"last_success_at\":\"$(now_utc)\""; fi
    record_ops_status ops.restore_drill_status "$patch}"
  fi
  [ -z "$WORKDIR" ] || rm -rf "$WORKDIR"
  exit "$status"
}
trap on_exit EXIT

[ -n "$source_url" ] || die "no source database: pass --source-url or set DATABASE_URL"
OPS_STATUS_DATABASE_URL="${OPS_STATUS_DATABASE_URL:-$source_url}"
case "$verify_objects" in '' | *[!0-9]*) die "--verify-objects needs a number" ;; esac
require_cmd psql pg_dump pg_restore sha256sum awk
umask 077
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/africasignal-drill.XXXXXX")"
pgpass_init # database passwords go in a private file, not on the command line

src="$(libpq_url "$source_url")"
src_db="$(url_dbname "$src")"
[ -n "$src_db" ] || die "cannot read the database name from the source URL"
psql "$src" -Atq -c "SELECT 1" >/dev/null 2>&1 || die "cannot connect to the source database"

scratch_db="$(printf '%s_drill_%s' "$src_db" "$(date -u +%Y%m%dt%H%M%Sz)")"
case "$scratch_db" in *[!A-Za-z0-9_]*) die "source database name '$src_db' must be letters, digits and underscores" ;; esac
[ "$scratch_db" != "$src_db" ] || die "scratch database would be the source database"
scratch_url="$(printf '%s' "$src" | sed -E "s#^([^:]+://[^/]*/)[^/?]*#\\1$scratch_db#")"
maint_url="$(url_with_maintenance_db "$src")"
[ "$(url_dbname "$scratch_url")" = "$scratch_db" ] || die "could not build the scratch database URL"

# ---- helpers -------------------------------------------------------------------------------

# Public tables (not extension tables such as spatial_ref_sys), one per line.
list_tables() {
  psql "$1" -v ON_ERROR_STOP=1 -Atq -c "
    SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r','p') AND n.nspname = 'public'
      AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')
    ORDER BY 1"
}

# table_counts URL: "table<TAB>rows" for every public table.
table_counts() {
  local url="$1" t n
  while IFS= read -r t; do
    n="$(psql "$url" -v ON_ERROR_STOP=1 -Atq -c "SELECT count(*) FROM public.\"$t\"")"
    printf '%s\t%s\n' "$t" "$n"
  done < <(list_tables "$url")
}

# table_digest URL TABLE: md5 of the table's rows as text, or nothing when the table is absent.
table_digest() {
  [ -n "$(psql "$1" -Atq -c "SELECT to_regclass('public.\"$2\"')")" ] || return 0
  psql "$1" -v ON_ERROR_STOP=1 -Atq -c \
    "SELECT md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) FROM public.\"$2\" t"
}

digest_tables="${DRILL_DIGEST_TABLES:-source evidence_document measurement assessment_version place}"
digests() {
  local url="$1" t
  for t in $digest_tables; do
    printf '%s\t%s\n' "$t" "$(table_digest "$url" "$t")"
  done
}

extensions() { psql "$1" -v ON_ERROR_STOP=1 -Atq -c "SELECT extname FROM pg_extension ORDER BY 1"; }
alembic_version() { psql "$1" -Atq -c "SELECT version_num FROM alembic_version" 2>/dev/null || true; }

# ---- take or pick the backup ---------------------------------------------------------------

log "source database: $src_db; scratch database: $scratch_db; mode: $mode"
before_counts="$WORKDIR/counts.before"
after_counts="$WORKDIR/counts.after"
restored_counts="$WORKDIR/counts.restored"
before_digests="$WORKDIR/digests.before"
after_digests="$WORKDIR/digests.after"

restore_args=(--target-url "$scratch_url" --recreate --overwrite-live)
[ "$verify_objects" -eq 0 ] || restore_args+=(--verify-objects "$verify_objects")

if [ "$mode" = fresh ]; then
  table_counts "$src" >"$before_counts"
  digests "$src" >"$before_digests"
  log "taking a fresh backup"
  DATABASE_URL="$source_url" "$here/backup.sh" --no-objects 2>&1 | tee "$WORKDIR/backup.log" >&2 ||
    die "the backup step failed: $(last_error "$WORKDIR/backup.log")"
  backup_used="$(sed -n 's/.*uploaded db\/\(africasignal-[^ ]*\) (.*/\1/p' "$WORKDIR/backup.log" | tail -n 1)"
  [ -n "$backup_used" ] || die "could not tell which dump the backup step wrote"
  table_counts "$src" >"$after_counts"
  digests "$src" >"$after_digests"
  restore_args+=(--backup "$backup_used")
else
  restore_args+=(--backup latest)
fi

# ---- restore -------------------------------------------------------------------------------

restore_started="$(date +%s)"
"$here/restore.sh" "${restore_args[@]}" 2>&1 | tee "$WORKDIR/restore.log" >&2 || die "the restore step failed: $(last_error "$WORKDIR/restore.log")"
restore_seconds=$(($(date +%s) - restore_started))
if [ "$mode" = latest ]; then
  backup_used="$(sed -n 's/.*restore ok: \(africasignal-[^ ]*\) ->.*/\1/p' "$WORKDIR/restore.log" | tail -n 1)"
fi
log "restored ${backup_used:-the backup} in ${restore_seconds}s"

# ---- compare -------------------------------------------------------------------------------

table_counts "$scratch_url" >"$restored_counts"
restored_version="$(alembic_version "$scratch_url")"
source_version="$(alembic_version "$src")"
[ -n "$restored_version" ] || die "the restored database has no alembic version"

# Extensions first: a dump restored without PostGIS would fail above, but check the list anyway.
if [ "$(extensions "$src")" != "$(extensions "$scratch_url")" ]; then
  die "extensions differ: source [$(extensions "$src" | tr '\n' ' ')] restored [$(extensions "$scratch_url" | tr '\n' ' ')]"
fi
if extensions "$scratch_url" | grep -qx postgis; then
  psql "$scratch_url" -v ON_ERROR_STOP=1 -Atq -c "SELECT postgis_version()" >/dev/null ||
    die "PostGIS does not work in the restored database"
fi

# Every id sequence must be at or past the highest id, or the restored database cannot take inserts.
psql "$scratch_url" -v ON_ERROR_STOP=1 -q >/dev/null <<'SQL' || die "an id sequence in the restored database is behind its table (see above)"
DO $$
DECLARE r record; seq text; max_id bigint; last_v bigint;
BEGIN
  FOR r IN
    SELECT c.relname FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
      JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'id' AND NOT a.attisdropped
     WHERE c.relkind = 'r'
  LOOP
    seq := pg_get_serial_sequence(format('public.%I', r.relname), 'id');
    CONTINUE WHEN seq IS NULL;
    EXECUTE format('SELECT max(id) FROM public.%I', r.relname) INTO max_id;
    EXECUTE format('SELECT CASE WHEN is_called THEN last_value ELSE last_value - 1 END FROM %s', seq) INTO last_v;
    IF max_id > last_v THEN
      RAISE EXCEPTION 'sequence % is at % but % has id %', seq, last_v, r.relname, max_id;
    END IF;
  END LOOP;
END $$;
SQL

problems="$WORKDIR/problems"
: >"$problems"

if [ "$mode" = fresh ]; then
  [ "$restored_version" = "$source_version" ] ||
    echo "alembic version: source $source_version, restored $restored_version" >>"$problems"
  awk -F'\t' '
    FILENAME == ARGV[1] { before[$1] = $2; seen[$1] = 1; next }
    FILENAME == ARGV[2] { after[$1] = $2; seen[$1] = 1; next }
    { restored[$1] = $2 }
    END {
      for (t in seen) {
        b = (t in before) ? before[t] : after[t]; a = (t in after) ? after[t] : before[t]
        if (!(t in restored)) { print "table " t " is missing from the restored database"; continue }
        lo = (b + 0 < a + 0) ? b + 0 : a + 0; hi = (b + 0 > a + 0) ? b + 0 : a + 0
        r = restored[t] + 0
        if (r < lo || r > hi) {
          printf "table %s: source has %s rows (before %s, after %s), restored has %s\n", t, (lo == hi ? lo : lo ".." hi), b, a, r
        } else n++
      }
      for (t in restored) if (!(t in seen)) print "table " t " exists only in the restored database"
      print "compared " n + 0 " tables" > "/dev/stderr"
    }' "$before_counts" "$after_counts" "$restored_counts" >>"$problems" 2>"$WORKDIR/awk.err"
  compared="$(sed -n 's/^compared \([0-9]*\) tables$/\1/p' "$WORKDIR/awk.err")"
  compared="${compared:-0}"

  # Content: only for tables whose count did not move and whose digest did not change while the dump ran.
  restored_digests="$WORKDIR/digests.restored"
  digests "$scratch_url" >"$restored_digests"
  skipped=""
  while IFS=$'\t' read -r t d_before; do
    d_after="$(awk -F'\t' -v t="$t" '$1 == t { print $2 }' "$after_digests")"
    d_restored="$(awk -F'\t' -v t="$t" '$1 == t { print $2 }' "$restored_digests")"
    [ -n "$d_before" ] || continue # the table does not exist in this database
    if [ "$d_before" != "$d_after" ]; then
      skipped="$skipped $t"
    elif [ "$d_before" != "$d_restored" ]; then
      echo "table $t: the rows differ between the source and the restored database (same count, different content)" >>"$problems"
    fi
  done <"$before_digests"
  [ -z "$skipped" ] || log "content of$skipped changed while the dump ran, so only its row count was compared"
else
  log "schema version: source ${source_version:-none}, restored $restored_version"
  compared=0
  while IFS=$'\t' read -r t n_restored; do
    compared=$((compared + 1))
    n_source="$(psql "$src" -Atq -c "SELECT count(*) FROM public.\"$t\"" 2>/dev/null || echo '')"
    log "rows in $t: source ${n_source:-n/a}, backup $n_restored"
  done <"$restored_counts"
  if [ "$restored_version" = "$source_version" ]; then
    # The schemas should match; a table the source has and the backup lacks means a bad backup.
    while IFS= read -r t; do
      awk -F'\t' -v t="$t" '$1 == t { found = 1 } END { exit !found }' "$restored_counts" || echo "table $t is missing from the restored database" >>"$problems"
    done < <(list_tables "$src")
  else
    log "warning: the backup is at schema $restored_version but the source is at $source_version (a migration since the backup?); tables not compared"
  fi
  for t in $digest_tables; do
    n_source="$(psql "$src" -Atq -c "SELECT count(*) FROM public.\"$t\"" 2>/dev/null || echo 0)"
    n_restored="$(awk -F'\t' -v t="$t" '$1 == t { print $2 }' "$restored_counts")"
    if [ "${n_source:-0}" -gt 0 ] && [ "${n_restored:-0}" -eq 0 ] && [ -n "$n_restored" ]; then
      log "warning: $t is empty in the backup but has $n_source rows now"
    fi
  done
fi

if [ -s "$problems" ]; then
  while IFS= read -r line; do log "MISMATCH: $line"; done <"$problems"
  die "restored database does not match the source: $(head -n 1 "$problems") ($(wc -l <"$problems" | tr -d ' ') problem(s))"
fi

log "restore drill ok: ${backup_used:-backup} restored in ${restore_seconds}s, $compared table(s) compared, schema $restored_version"
