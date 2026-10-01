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
   Then sign in to the operator console, open **Settings**, and fill in **Backup storage**: endpoint
   URL, bucket, access key ID, secret access key, and optionally the key prefix and the days to keep
   dumps. **Evidence storage** on the same page is the app's bucket, which the backup also reads.
   Changes apply the next time a backup or restore runs.
2. Create a heartbeat check with a dead-man's-switch service (for example healthchecks.io): expected
   every 24 hours, grace 2 hours, alerts by email. Copy its ping URL. This is how a *missed* night gets
   noticed; a failed one also pings `<url>/fail`.
3. Set these in the server's `.env` (next to `docker-compose.yml`). Only these are needed there; the
   bucket settings come from the console:

   | Variable | Meaning |
   |---|---|
   | `ENV` | `staging` or `production`; also the default bucket prefix `africasignal/<ENV>/` |
   | `DATABASE_URL`, `SECRET_KEY` | the same values the app uses. The backup job reads the console settings from that database, and decrypts the saved secrets with `SECRET_KEY` |
   | `BACKUP_AT_UTC` | optional; `HH:MM`, default `02:30` |
   | `BACKUP_PASSPHRASE` or `BACKUP_PASSPHRASE_FILE` | optional; encrypts dumps |
   | `BACKUP_HEARTBEAT_URL` | the ping URL from step 2 |

   Where a value is set, the console wins over `.env`: for each of `backup_s3_endpoint_url`,
   `backup_s3_bucket`, `backup_s3_access_key_id`, `backup_s3_secret_access_key`, `backup_s3_prefix`,
   `backup_retain_days` and `s3_*` the job uses the console value, then the environment variable in
   capitals (`BACKUP_S3_BUCKET`, `BACKUP_RETAIN_DAYS`, `S3_BUCKET`, ...), then the default (30 days).
   The environment variables are the fallback when the console cannot be read, which is exactly the
   situation in a disaster recovery (4.1). The job logs a warning when it falls back.

4. Start the job and run one backup by hand to prove it works:

   ```sh
   docker compose --profile backup up -d --build backup
   docker compose --profile backup run --rm --entrypoint /opt/africasignal/scripts/backup.sh backup
   docker compose --profile backup run --rm --entrypoint /opt/africasignal/scripts/restore.sh backup --list
   ```

5. Put a reminder in the operator calendar to check the heartbeat service once a week and to do a
   restore drill every quarter.

Without Docker (for example on a developer machine) run `scripts/backup.sh` directly; it needs
`pg_dump` and `pg_restore` 16, `psql`, `rclone`, `curl` and `openssl`, and `africasignal` installed
(`pip install .`) if it should read the console settings. Set `BACKUP_DIR=/some/dir` instead of the
bucket settings to back up to a local directory, and `OPS_USE_CONSOLE_SETTINGS=0` to ignore the console.

## 3. Daily and weekly checks

- The heartbeat service is green. If it alerts, see section 6.
- The audit log (console, **Audit log**) has no open `alert.opened` for `backup_stale` or
  `restore_drill_failed` (section 7).
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
   **`SECRET_KEY` must be the old value**: secrets saved in the console (API keys, bucket credentials)
   are encrypted with a key derived from it and cannot be read with another.
2. Start only the database: `docker compose up -d db`. Wait until it is healthy.
   The console settings live in the database you are about to restore, so they cannot tell the restore
   where the backups are. **Put the backup bucket's endpoint, bucket, access key and secret in your
   password manager, and export them as `BACKUP_S3_ENDPOINT_URL`, `BACKUP_S3_BUCKET`,
   `BACKUP_S3_ACCESS_KEY_ID`, `BACKUP_S3_SECRET_ACCESS_KEY` (and `BACKUP_S3_PREFIX` if you set one) in
   `.env` before step 3.** `restore.sh` then logs a warning that the console cannot be read and uses them.
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

Restore into a scratch database (4.3.3, step 2), then copy what you need with `psql` or
`pg_dump --table ... | psql`. Do not restore over the live database for a partial loss.

### 4.3 Restore drill

Purpose: prove the backups can be restored, and time it. Needed before launch (AS-041, AS-043) and
then every quarter. There are two parts: an automated drill that needs only a PostgreSQL with PostGIS
(below), and a manual drill on a staging host with the real bucket (4.3.2), which is what AS-041 is
accepted on.

#### 4.3.1 Automated drill (`scripts/restore-drill.sh`)

The script takes a fresh backup of a database with `scripts/backup.sh`, restores it with
`scripts/restore.sh` into a scratch database on the same server (`<name>_drill_<timestamp>`), and
compares the two. It fails with a non-zero exit and the reason when any of these differ:

- the set of tables, and every table's row count (a source that is being written to during the drill
  may move within the range seen before and after the dump, not outside it);
- the alembic version and the list of PostgreSQL extensions, and PostGIS must answer `postgis_version()`;
- the content (an md5 over every row) of `source`, `evidence_document`, `measurement`,
  `assessment_version` and `place`, when the source did not change while the dump ran;
- an id sequence behind its table's highest id (the restored database could not take inserts).

It then drops the scratch database (`--keep` keeps it) and records the result in the source database
(`ops.restore_drill_status`), where the `check_backups` job (section 7) turns a failure into an alert.
The scratch database is created with the source's own credentials, so the role needs `CREATEDB`.

Run it in any of these places; none needs a staging host or a bucket:

```sh
# CI: the "Restore drill" step of .github/workflows/ci.yml does this on every pull request, on the
# migrated and seeded schema. The fault-injection tests are tests/ops/test_restore_drill.py.

# Docker Compose (the database from the compose file; dumps go to a throwaway directory):
docker compose --profile drill run --rm restore-drill

# A developer machine with PostgreSQL 16 + PostGIS, rclone and openssl installed:
BACKUP_DIR=/tmp/drill-backups OPS_USE_CONSOLE_SETTINGS=0 \
  scripts/restore-drill.sh --source-url postgresql://africasignal:africasignal@localhost:5432/africasignal
```

Good output ends with `restore drill ok: africasignal-<stamp>.dump restored in <n>s, <n> table(s)
compared, schema <version>`. Anything else is a failed drill: the line starting `MISMATCH:` or `ERROR:`
says what. A drill that has never been run against the real bucket proves the tooling, not the
backups.

#### 4.3.2 Drill on a staging host, with the real bucket (still to do)

There is no staging host and no backup bucket yet (plan decisions D4 and D5), so this has not been
done. When both exist:

1. Give the staging host the same `BACKUP_S3_*` settings and `BACKUP_PASSPHRASE_FILE` as production (or a
   bucket holding a copy of production's dumps), and the staging database's `DATABASE_URL`. Use a
   scratch bucket for `RESTORE_S3_*`, never the app's bucket.
2. Run the automated drill against staging's own database, to prove the tooling end to end with the
   real bucket and encryption (an empty `DRILL_BACKUP_DIR` means "use the bucket, not a local
   directory"):

   ```sh
   DRILL_BACKUP_DIR= docker compose --profile drill run --rm restore-drill
   ```

3. Run it against the newest real backup, and check that sampled evidence objects exist in the
   scratch bucket:

   ```sh
   DRILL_BACKUP_DIR= docker compose --profile drill run --rm restore-drill --latest --verify-objects 20
   ```

   `--latest` can only compare what a backup hours old allows: it restores, the schema version is
   present, every table the source has is present (when the schema versions match), key tables are
   not empty when the source's are, and row counts are printed rather than compared.
4. Do the manual look below, for the parts a script cannot judge, and fill in the drill record.

#### 4.3.3 Manual steps on staging

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
| 2026-09-30 | fresh backup, local directory | automated (`restore-drill.sh`), PostgreSQL 16 + PostGIS on a developer machine | 2 s | Passed: 30 tables, counts and content equal, sequences ok | Proves the scripts and the schema. Not a staging drill: no bucket, no encryption, no second app copy. |
| | | | | **No drill on a staging host has been done yet.** | No staging host or backup bucket exists (plan decisions D4 and D5). AS-041 is accepted only when a row for the staging drill (4.3.2 and 4.3.3) is filled in. |

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
| `DATABASE_URL is not set` | Missing variable in `.env`; fix and `docker compose --profile backup up -d backup`. |
| `set BACKUP_S3_BUCKET` | No backup bucket in the console (**Settings > Backup storage**) or in `.env`. |
| `warning: console settings could not be read (...)` | The job could not read the console settings (database down, wrong `SECRET_KEY`, schema not migrated) and used `.env` instead. Fine in a disaster recovery; otherwise fix the cause, because a bucket changed in the console is ignored until it works. |
| `warning: africasignal is not importable here` | The script runs without the app installed (not the backup image), so it uses `.env` only. |
| `pg_dump: error: ... connection` | The database is down or the password changed. Check `docker compose ps db`. |
| `pg_dump: error: aborting because of server version mismatch` | The server is newer than the client (16). Rebuild the backup image with the matching `postgres:<major>` base in `docker/backup/Dockerfile`. |
| `uploaded size ... does not match` or rclone errors | Network or credentials for the backup bucket. Test with `restore --list`. |
| `the backup bucket is the app's own bucket` | The backup bucket setting equals the evidence storage setting. Use a separate bucket. |
| `immutable file modified` | An object in the app bucket has a different size from its backup copy. Evidence objects must never change: find out why (someone edited or overwrote it) before continuing. |
| Missed night, no error | The container is not running (`docker compose --profile backup ps`) or the host was down. Start it and run a backup by hand. |

A failed night costs one day of recovery point; a second failed night puts you past the 24-hour
target, so fix it the same day.

## 7. Monitoring and alerts

### 7.1 Backup and restore-drill alerts (automated)

The scheduler runs `check_backups` every hour. `backup.sh` and `restore-drill.sh` leave their last
result in the app database (settings `ops.backup_status` and `ops.restore_drill_status`; set
`OPS_RECORD_STATUS=0` to stop that). The job raises:

| Alert | When | Clears when |
|---|---|---|
| `backup_stale` | The last successful backup is older than **Settings > Backup storage > Alert when the last backup is older than** (default 36 hours), or, outside development, none has been recorded in that long since the job first ran. | A backup succeeds. |
| `restore_drill_failed` | The last restore drill did not pass (the alert text says why). | A later drill passes. |

Where to see them: the console's **Audit log** has an `alert.opened` row (operator "system") when an
alert opens and `alert.resolved` when it clears, and the app logs an `ALERT ... still open` error line
every hour while it stays open. The current state is the `ops.alert.backup_stale` and
`ops.alert.restore_drill_failed` rows of the `setting` table:

```sql
SELECT key, value->>'state' AS state, value->>'opened_at' AS opened, value->>'summary' AS summary
FROM setting WHERE key LIKE 'ops.alert.%';
```

Nothing is sent to anyone: no operator notification channel exists yet (email goes only to readers).
Until one does, watch the log or the audit log, and keep the external heartbeat service (section 2) as
the thing that actually wakes a person. A backup job that is down and cannot even record its own
failure shows up here as `backup_stale` once the limit passes.

### 7.2 Uptime and other alerts (manual)

The plan (AS-041) asks for an uptime check on `/healthz` and email alerts when a source is failing,
jobs are dead, or the LLM budget is 80 % spent. **Those are not automated yet** (only the backup alerts
above are). Until they exist, do the following.

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

## 8. Console access and client addresses

**Client addresses behind a proxy.** The rate limits (sign-in, feedback, API, "use my location" and
console sign-in) are keyed on the reader's address. Behind a reverse proxy or CDN the connecting
address is the proxy's, so every reader would share one limit. In the console, **Settings > Website >
Proxies in front of the site** says how many proxies add to `X-Forwarded-For` (1 for one proxy, 2 for
a CDN and a proxy). The app then takes that many entries from the right end of the header, which only
your own proxies wrote; anything a reader adds further left is ignored. Leave it at 0 when nothing
sits in front of the app, and also when the proxy runs on the same machine and uvicorn already
handles it (its default trusts `127.0.0.1`; do not set `FORWARDED_ALLOW_IPS=*`). After deploying,
check that two different readers get separate limits (the launch checklist has this step).

**A stolen console cookie.** Signing out ends every session of that operator, in every browser. To end
sessions of an operator who cannot sign out: `python -m africasignal.admin revoke-sessions --email ...`.
A password change or disabling the operator also ends them.

**An operator locked out.** Sign-in is stopped for a client after 5 failures in 15 minutes, and for
everyone after 50 failures on one account in 15 minutes (refused attempts are not counted, so the lock
never gets longer by being tried). To let the operator in at once:
`python -m africasignal.admin unlock-operator --email ...`. Failures and lockouts are in the audit log
(`operator.sign_in_failed`, `operator.sign_in_locked`).
