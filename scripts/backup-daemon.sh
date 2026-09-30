#!/usr/bin/env bash
# Runs scripts/backup.sh once a day at BACKUP_AT_UTC (HH:MM, default 02:30 UTC). This is the
# command of the `backup` compose service. A failed run is logged and alerted by backup.sh itself
# (failure heartbeat); the loop carries on so one bad night does not stop the next.
set -Eeuo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
at="${BACKUP_AT_UTC:-02:30}"
case "$at" in [0-2][0-9]:[0-5][0-9]) ;; *) echo "BACKUP_AT_UTC must be HH:MM, got '$at'" >&2; exit 2 ;; esac

seconds_until_next() {
  local now next
  now="$(date -u +%s)"
  next="$(date -u -d "today $at" +%s)"
  [ "$next" -gt "$now" ] || next="$(date -u -d "tomorrow $at" +%s)"
  echo $((next - now))
}

[ -z "${BACKUP_ON_START:-}" ] || "$here/backup.sh" || true
while true; do
  wait_s="$(seconds_until_next)"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [backup-daemon] next backup in ${wait_s}s (at $at UTC)" >&2
  sleep "$wait_s"
  "$here/backup.sh" || true
done
