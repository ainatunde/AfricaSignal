# AfricaSignal runbook: backup, restore and operations

Scope: what is backed up, how to check it, how to restore, and the restore drill that must be done
before launch (AS-041). Targets from the implementation plan (B12): recovery point 24 hours,
recovery time 4 hours, dumps kept 30 days.

## 1. What is backed up

| What | How | Where |
|---|---|---|
| PostgreSQL database (all tables, including emails of signed-in users) | `pg_dump --format=custom` by `scripts/backup.sh`, nightly at 02:30 UTC | `db/africasignal-<UTC timestamp>.dump` (or `.dump.enc`) plus a `.sha256` file, in the backup bucket |
| Evidence objects (captured source documents in the app bucket) | `rclone copy` of the whole app bucket | `objects/` in the backup bucket |

Not backed up: the `.env` file and secrets (keep them in your password manager, not in the
repository), container images (rebuilt from the repository), and container logs.

Design choices worth knowing:

- **The backup bucket is separate from the app bucket**, ideally in a different provider account,
  with credentials that only the backup job holds. `backup.sh` refuses to run if both are the same bucket.
- **Objects are copied, never synced.** Deleting an object in the app bucket (a takedown, a retention
  expiry) does not delete its backup. The copy also fails loudly if an existing backup object has a
  different size from the source, because evidence objects are named by their SHA-256 and never change.
  (A takedown that must also remove the backup copy is a manual step: delete the object under
  `objects/` in the backup bucket, and dumps older than 30 days age out on their own.)
- **Old dumps are pruned only after the new one is uploaded and its size checked**, so a run of
  failures never empties the store.
- **A dump is checked before upload** (`pg_restore --list` must read it) and **after restore** (schema
  version present, key tables present, row counts printed).
- Dumps hold email addresses. Keep the backup bucket private. Set `BACKUP_PASSPHRASE_FILE` (or
  `BACKUP_PASSPHRASE`) to also encrypt them (AES-256). **If you encrypt, store the passphrase somewhere
  other than the server, or the backups are useless when the server is lost.**

## 2. One-time setup

1. Create a private bucket for backups in a different account from the app bucket (Cloudflare R2,
   Backblaze B2 or any S3-compatible store). Create an access key scoped to that bucket only, with read,
   write, list and delete (delete is for pruning).
2. Create a heartbeat check with a dead-man's-switch service (for example healthchecks.io): expected
   every 24 hours, grace 2 hours, alerts by email. Copy its ping URL. This is how a *missed* night gets
   noticed; a failed one also pings `<url>/fail`.
3. Set these in the server's `.env` (next to `docker-compose.yml`):

   | Variable | Meaning |
   |---|---|
   | `ENV` | `staging` or `production`; also the default bucket prefix `africasignal/<ENV>/` |
   | `DATABASE_URL`, `S3_*` | the same values the app uses |
   | `BACKUP_S3_ENDPOINT_URL`, `BACKUP_S3_BUCKET`, `BACKUP_S3_ACCESS_KEY_ID`, `BACKUP_S3_SECRET_ACCESS_KEY` | the backup bucket |
   | `BACKUP_S3_PREFIX` | optional; default `africasignal/<ENV>` |
   | `BACKUP_RETAIN_DAYS` | optional; default 30 |
   | `BACKUP_AT_UTC` | optional; `HH:MM`, default `02:30` |
   | `BACKUP_PASSPHRASE` or `BACKUP_PASSPHRASE_FILE` | optional; encrypts dumps |
   | `BACKUP_HEARTBEAT_URL` | the ping URL from step 2 |

4. Start the job and run one backup by hand to prove it works:

   ```sh
   docker compose --profile backup up -d --build backup
   docker compose --profile backup run --rm --entrypoint /opt/africasignal/scripts/backup.sh backup
   docker compose --profile backup run --rm --entrypoint /opt/africasignal/scripts/restore.sh backup --list
   ```

5. Put a reminder in the operator calendar to check the heartbeat service once a week and to do a
   restore drill every quarter.

Without Docker (for example on a developer machine) run `scripts/backup.sh` directly; it needs
`pg_dump` and `pg_restore` 16, `psql`, `rclone`, `curl` and `openssl`. Set `BACKUP_DIR=/some/dir`
instead of the `BACKUP_S3_*` variables to back up to a local directory.

## 3. Daily and weekly checks

- The heartbeat service is green. If it alerts, see section 6.
- `docker compose --profile backup logs --tail 50 backup` ends with `backup ok: ... N dump(s) kept`.
- Weekly: `restore.sh --list` shows a dump from each of the last 7 nights and sizes that do not shrink
  suddenly.

## 4. Restore

`scripts/restore.sh` never has a default target and refuses to touch the live database unless told
twice (`--overwrite-live`, and always so when `ENV=production`). It verifies the checksum, decrypts,
restores in one transaction (all or nothing), then checks the schema version and table contents.

Run it inside the backup container so it has the right tools and credentials:

```sh
alias restore='docker compose --profile backup run --rm --entrypoint /opt/africasignal/scripts/restore.sh backup'
restore --list
```

### 4.1 Disaster recovery (the server or its database is lost)

1. Provision a host and install Docker. Get the repository and the `.env` from your password manager.
2. Start only the database: `docker compose up -d db`. Wait until it is healthy.
3. Restore the newest dump into it and copy the objects back into the app bucket (if the app bucket
   was lost too, create it first):

   ```sh
   restore --target-url postgresql://africasignal:<password>@db:5432/africasignal --recreate \
     --overwrite-live --restore-objects --verify-objects 20
   ```

   For the object copy, set `RESTORE_S3_ENDPOINT_URL`, `RESTORE_S3_BUCKET`, `RESTORE_S3_ACCESS_KEY_ID`
   and `RESTORE_S3_SECRET_ACCESS_KEY` to the app bucket. (`restore.sh` never writes objects anywhere
   you did not name.)
4. Start everything: `docker compose up -d`. The `migrate` service is a no-op when the schema is current.
5. Check `curl https://<domain>/healthz` returns `{"ok": true, "db": true}`, open a few situation
   pages, and check the operator console (Jobs, Sources).
6. Anything that happened after the last dump (up to 24 hours) is gone. Sources are fetched again on
   their schedule; if the operator approved source permissions or published assessments that day,
   repeat those actions.

### 4.2 Restore a single table or recent data

Restore into a scratch database (4.3, step 2), then copy what you need with `psql` or
`pg_dump --table ... | psql`. Do not restore over the live database for a partial loss.

### 4.3 Restore drill (staging)

Purpose: prove the backups can be restored, on a host that is not production, and time it. Needed
before launch (AS-041, AS-043) and then every quarter.

1. On staging, make sure a recent backup exists: run a backup by hand (section 2, step 4) and note the
   time.
2. Restore into a new database on the staging database server, leaving the live staging database alone,
   and into a scratch bucket:

   ```sh
   export RESTORE_S3_ENDPOINT_URL=... RESTORE_S3_BUCKET=africasignal-drill \
     RESTORE_S3_ACCESS_KEY_ID=... RESTORE_S3_SECRET_ACCESS_KEY=...
   restore --target-url postgresql://africasignal:<password>@db:5432/africasignal_drill --recreate \
     --restore-objects --verify-objects 20
   ```

   Expected: `checksum ok`, `schema version: <current>`, row counts that match staging, `sampled 20
   evidence object(s): all present`, and `restore ok`.
3. Start a second copy of the app pointed at the restored database and look at it:

   ```sh
   docker compose run -d --name drill-web -p 8001:8000 \
     -e DATABASE_URL=postgresql+psycopg://africasignal:<password>@db:5432/africasignal_drill web
   curl http://localhost:8001/healthz          # {"ok": true, "db": true}
   ```

   `/healthz` only proves the app can reach the database, so also open `/` and one situation page in a
   browser (through an SSH tunnel to port 8001) and check they show the data you expect. A scripted
   smoke test of the public pages belongs to the launch checklist (AS-043) and can replace this
   manual look once it exists.
4. Clean up: `docker rm -f drill-web`, `DROP DATABASE africasignal_drill`, empty the scratch bucket.
5. Record the result in the table below: date, backup used, who ran it, how long the restore took
   (start of step 2 to a working page in step 3), and anything that went wrong.

### Drill record

| Date | Backup used | Operator | Restore time | Result | Notes |
|---|---|---|---|---|---|
| | | | | **No drill has been done yet.** | The scripts were tested against a local PostgreSQL 16 with PostGIS and an S3-compatible test server, but not on a staging host (no staging host or backup bucket exists yet; plan decisions D4 and D5). AS-041 is accepted only when the first row is filled in. |

## 5. Logs

- All services log to the container's JSON log on the host, rotated by Docker: 5 files of 10 MB per
  service (`x-logging` in `docker-compose.yml`). Read them with `docker compose logs --tail 200 <service>`.
- Application logs are JSON lines with `ts`, `level`, `logger` and `msg`.
- To ship logs to a hosted service instead, change the `driver` in `x-logging` (for example `syslog`,
  `fluentd`, or a service's Docker logging plugin) and run `docker compose up -d`. Keep local rotation
  as the fallback if the plugin's destination is unreachable.
- Backup and restore also append to `BACKUP_LOG_FILE` / `RESTORE_LOG_FILE` when those are set.

## 6. When a backup fails or is missed

The job exits non-zero and pings `<heartbeat>/fail` on any failure. Read
`docker compose --profile backup logs --tail 100 backup`, then:

| Message | Cause and fix |
|---|---|
| `DATABASE_URL is not set`, `set BACKUP_S3_BUCKET` | Missing variable in `.env`; fix and `docker compose --profile backup up -d backup`. |
| `pg_dump: error: ... connection` | The database is down or the password changed. Check `docker compose ps db`. |
| `pg_dump: error: aborting because of server version mismatch` | The server is newer than the client (16). Rebuild the backup image with the matching `postgres:<major>` base in `docker/backup/Dockerfile`. |
| `uploaded size ... does not match` or rclone errors | Network or credentials for the backup bucket. Test with `restore --list`. |
| `the backup bucket is the app's own bucket` | `BACKUP_S3_BUCKET`/`BACKUP_S3_ENDPOINT_URL` equals the app's. Use a separate bucket. |
| `immutable file modified` | An object in the app bucket has a different size from its backup copy. Evidence objects must never change: find out why (someone edited or overwrote it) before continuing. |
| Missed night, no error | The container is not running (`docker compose --profile backup ps`) or the host was down. Start it and run a backup by hand. |

A failed night costs one day of recovery point; a second failed night puts you past the 24-hour
target, so fix it the same day.

## 7. Monitoring and alerts

The plan (AS-041) asks for an uptime check on `/healthz` and email alerts when a source is failing,
jobs are dead, or the LLM budget is 80 % spent. **This change does not automate those; it only ships
the backup scripts and this runbook.** Until they exist, do the following.

- **Uptime:** point an external monitor (UptimeRobot, Better Stack or similar) at
  `https://<domain>/healthz` with a 1-minute interval and email alerts. The body must contain
  `"ok": true` (or `"ok":true` depending on the JSON encoder).
- **By hand, daily** (or from a cron job that mails the output), run against the database:

  ```sql
  -- sources that are failing
  SELECT slug, health, consecutive_failures, last_success_at, last_error FROM source WHERE active AND health = 'failing';
  -- dead jobs (should be 0)
  SELECT kind, count(*), max(last_error) FROM job WHERE status = 'dead' GROUP BY kind;
  -- LLM spend today against LLM_DAILY_BUDGET_USD (default 10; alert at 8)
  SELECT coalesce(sum(cost_usd), 0) AS spent_today_usd FROM llm_call WHERE ts >= date_trunc('day', now() AT TIME ZONE 'UTC');
  ```

  `docker compose exec db psql -U africasignal africasignal` opens a prompt.
