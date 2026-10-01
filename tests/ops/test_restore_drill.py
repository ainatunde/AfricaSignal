"""scripts/restore-drill.sh and the status that backup.sh and the drill leave for the alert job.

Needs the same tools as test_backup_restore.py (PostgreSQL client and server, rclone, openssl).
A fault is injected by running a copy of the scripts whose restore.sh damages the restored
database just before it reports success, so the drill has something real to catch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.ops.test_backup_restore import ROOT, SCHEMA, libpq, psql, run, with_db

DRILL = ROOT / "scripts" / "restore-drill.sh"
BACKUP = ROOT / "scripts" / "backup.sh"

pytestmark = pytest.mark.skipif(
    any(shutil.which(c) is None for c in ("pg_dump", "pg_restore", "psql", "rclone", "openssl")),
    reason="needs pg_dump, pg_restore, psql, rclone and openssl",
)

SETTING = """
CREATE TABLE setting (key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
"""


@pytest.fixture(scope="module")
def admin_url() -> str:
    url = libpq(os.environ["DATABASE_URL"])
    try:
        psql(url, "SELECT 1")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("PostgreSQL is not reachable")
    return url


@pytest.fixture
def source_db(admin_url: str) -> Iterator[str]:
    """A populated source database with a setting table; drops it and any drill databases."""
    name = f"ops_drill_{uuid.uuid4().hex[:8]}"
    psql(admin_url, f'CREATE DATABASE "{name}"')
    url = with_db(admin_url, name)
    psql(url, SCHEMA + SETTING)
    yield url
    leftovers = psql(
        admin_url, f"SELECT datname FROM pg_database WHERE datname LIKE '{name}\\_drill\\_%'"
    ).split()
    for db in [name, *leftovers]:
        psql(admin_url, f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


def drill_dbs(admin_url: str, source: str) -> list[str]:
    name = source.rsplit("/", 1)[1]
    found = psql(
        admin_url, f"SELECT datname FROM pg_database WHERE datname LIKE '{name}\\_drill\\_%'"
    )
    return found.split()


def status(url: str, key: str) -> dict[str, object]:
    return json.loads(psql(url, f"SELECT value FROM setting WHERE key = '{key}'"))


def faulty_scripts(tmp_path: Path, damage_sql: str) -> Path:
    """A copy of scripts/ whose restore.sh runs ``damage_sql`` on the restored database."""
    copy = tmp_path / "scripts"
    shutil.copytree(ROOT / "scripts", copy)
    restore = copy / "restore.sh"
    text = restore.read_text()
    marker = 'log "restore ok:'
    assert marker in text
    restore.write_text(
        text.replace(marker, f'psql "$target" -v ON_ERROR_STOP=1 -q -c "{damage_sql}"\n{marker}', 1)
    )
    return copy / "restore-drill.sh"


def test_drill_passes_drops_its_scratch_database_and_records_the_result(
    tmp_path: Path, admin_url: str, source_db: str
) -> None:
    done = run(DRILL, "--source-url", source_db, env={"BACKUP_DIR": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert "restore drill ok" in done.stderr
    assert drill_dbs(admin_url, source_db) == []

    drill = status(source_db, "ops.restore_drill_status")
    assert drill["ok"] is True and drill["mode"] == "fresh"
    assert str(drill["backup"]).startswith("africasignal-") and drill["tables_compared"] == 8
    assert drill["detail"] == "" and "last_success_at" in drill
    backup = status(source_db, "ops.backup_status")  # the drill's own backup counts as a backup
    assert backup["last_success_name"] == drill["backup"]


def test_drill_keeps_the_scratch_database_when_asked(
    tmp_path: Path, admin_url: str, source_db: str
) -> None:
    done = run(DRILL, "--source-url", source_db, "--keep", env={"BACKUP_DIR": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    [kept] = drill_dbs(admin_url, source_db)
    assert psql(with_db(admin_url, kept), "SELECT count(*) FROM source") == "2"


def test_drill_takes_the_source_from_database_url_and_can_skip_recording(
    tmp_path: Path, source_db: str
) -> None:
    env = {"BACKUP_DIR": str(tmp_path), "DATABASE_URL": source_db, "OPS_RECORD_STATUS": "1"}
    done = run(DRILL, "--no-record", env=env)
    assert done.returncode == 0, done.stderr
    assert (
        psql(source_db, "SELECT count(*) FROM setting WHERE key = 'ops.restore_drill_status'")
        == "0"
    )


def test_drill_needs_a_source(tmp_path: Path) -> None:
    done = run(DRILL, env={"BACKUP_DIR": str(tmp_path), "DATABASE_URL": ""})
    assert done.returncode != 0 and "no source database" in done.stderr


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("DELETE FROM source WHERE id = 1", "table source: source has 2 rows"),
        ("UPDATE source SET name = 'changed' WHERE id = 1", "the rows differ"),
        ("INSERT INTO job DEFAULT VALUES", "table job: source has 0 rows"),
        ("UPDATE alembic_version SET version_num = '0002'", "alembic version: source 0003"),
        ("DROP TABLE measurement", "table measurement is missing"),
        ("SELECT setval('source_id_seq', 1)", "id sequence"),
    ],
)
def test_drill_fails_loudly_when_the_restored_database_differs(
    tmp_path: Path, admin_url: str, source_db: str, damage: str, expected: str
) -> None:
    drill = faulty_scripts(tmp_path, damage)
    done = run(drill, "--source-url", source_db, env={"BACKUP_DIR": str(tmp_path / "store")})
    assert done.returncode != 0, done.stderr
    assert expected in done.stderr
    assert drill_dbs(admin_url, source_db) == []  # still cleaned up

    drill_status = status(source_db, "ops.restore_drill_status")
    assert drill_status["ok"] is False
    assert drill_status["detail"]  # a reason is recorded
    assert "last_success_at" not in drill_status


def test_drill_failing_in_the_restore_step_records_the_reason(
    tmp_path: Path, source_db: str
) -> None:
    drill = faulty_scripts(tmp_path, "SELECT 1/0")
    done = run(drill, "--source-url", source_db, env={"BACKUP_DIR": str(tmp_path / "store")})
    assert done.returncode != 0
    failed = status(source_db, "ops.restore_drill_status")
    assert failed["ok"] is False and "division by zero" in str(failed["detail"])


def test_a_later_passing_drill_keeps_the_history_but_flips_ok(
    tmp_path: Path, source_db: str
) -> None:
    bad = faulty_scripts(tmp_path, "DELETE FROM source WHERE id = 1")
    store = {"BACKUP_DIR": str(tmp_path / "store")}
    assert run(bad, "--source-url", source_db, env=store).returncode != 0
    assert run(DRILL, "--source-url", source_db, env=store).returncode == 0
    now = status(source_db, "ops.restore_drill_status")
    assert now["ok"] is True and now["detail"] == ""


def test_source_that_changes_during_the_dump_may_move_within_the_bracket(
    tmp_path: Path, source_db: str
) -> None:
    # A row added by another writer between the counts before and after the dump is not a mismatch:
    # the restored count is allowed to be either side. Simulate by having the dump see the new row.
    drill = tmp_path / "scripts"
    shutil.copytree(ROOT / "scripts", drill)
    backup = drill / "backup.sh"
    text = backup.read_text()
    marker = "ping_heartbeat /start"
    assert marker in text
    backup.write_text(
        text.replace(
            marker,
            f'psql "$db_url" -q -c "INSERT INTO source (name) VALUES (\'late\')"\n{marker}',
            1,
        )
    )
    done = run(
        drill / "restore-drill.sh",
        "--source-url",
        source_db,
        env={"BACKUP_DIR": str(tmp_path / "s")},
    )
    assert done.returncode == 0, done.stderr
    assert "changed while the dump ran" in done.stderr


def test_latest_mode_restores_the_newest_existing_backup(
    tmp_path: Path, admin_url: str, source_db: str
) -> None:
    env = {"BACKUP_DIR": str(tmp_path)}
    assert run(BACKUP, env={**env, "DATABASE_URL": source_db}).returncode == 0
    psql(source_db, "INSERT INTO source (name) VALUES ('after the backup')")  # the source moved on
    done = run(DRILL, "--source-url", source_db, "--latest", env=env)
    assert done.returncode == 0, done.stderr
    assert "rows in source: source 3, backup 2" in done.stderr
    assert status(source_db, "ops.restore_drill_status")["mode"] == "latest"
    assert drill_dbs(admin_url, source_db) == []


def test_latest_mode_fails_without_a_backup(tmp_path: Path, source_db: str) -> None:
    done = run(DRILL, "--source-url", source_db, "--latest", env={"BACKUP_DIR": str(tmp_path)})
    assert done.returncode != 0 and "no backups found" in done.stderr
    assert status(source_db, "ops.restore_drill_status")["ok"] is False


def test_latest_mode_catches_a_table_missing_from_the_backup(
    tmp_path: Path, source_db: str
) -> None:
    env = {"BACKUP_DIR": str(tmp_path)}
    assert run(BACKUP, env={**env, "DATABASE_URL": source_db}).returncode == 0
    psql(source_db, "CREATE TABLE added_later (id int)")  # same schema version, yet a new table
    done = run(DRILL, "--source-url", source_db, "--latest", env=env)
    assert done.returncode != 0 and "table added_later is missing" in done.stderr


def test_latest_mode_only_warns_when_the_schema_version_moved_on(
    tmp_path: Path, source_db: str
) -> None:
    env = {"BACKUP_DIR": str(tmp_path)}
    assert run(BACKUP, env={**env, "DATABASE_URL": source_db}).returncode == 0
    psql(
        source_db,
        "CREATE TABLE added_later (id int); UPDATE alembic_version SET version_num = '0004'",
    )
    done = run(DRILL, "--source-url", source_db, "--latest", env=env)
    assert done.returncode == 0, done.stderr
    assert "tables not compared" in done.stderr


def test_backup_records_success_and_failure_for_the_alert_job(
    tmp_path: Path, source_db: str
) -> None:
    env = {"DATABASE_URL": source_db, "BACKUP_DIR": str(tmp_path)}
    assert run(BACKUP, env=env).returncode == 0
    ok = status(source_db, "ops.backup_status")
    assert str(ok["last_success_name"]).startswith("africasignal-") and ok["last_success_bytes"]
    assert ok["last_success_at"].endswith("Z")  # type: ignore[attr-defined]

    bad = run(BACKUP, env={**env, "BACKUP_RETAIN_DAYS": "soon"})
    assert bad.returncode != 0
    failed = status(source_db, "ops.backup_status")
    assert "positive integer" in str(failed["last_failure_reason"])
    assert failed["last_success_at"] == ok["last_success_at"]  # the success is kept
    assert failed["last_failure_at"]


def test_backup_without_a_setting_table_or_with_recording_off_records_nothing(
    tmp_path: Path, admin_url: str
) -> None:
    name = f"ops_nosetting_{uuid.uuid4().hex[:8]}"
    psql(admin_url, f'CREATE DATABASE "{name}"')
    try:
        url = with_db(admin_url, name)
        psql(url, SCHEMA)  # no setting table
        done = run(BACKUP, env={"DATABASE_URL": url, "BACKUP_DIR": str(tmp_path)})
        assert done.returncode == 0 and "could not record" not in done.stderr
        psql(url, SETTING)
        off = run(
            BACKUP,
            env={"DATABASE_URL": url, "BACKUP_DIR": str(tmp_path), "OPS_RECORD_STATUS": "0"},
        )
        assert off.returncode == 0
        assert psql(url, "SELECT count(*) FROM setting") == "0"
    finally:
        psql(admin_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_json_str_escapes_what_a_failure_message_can_contain() -> None:
    lib = ROOT / "scripts" / "lib" / "ops_common.sh"
    text = 'said "no" \\ back\tslash\nnewline ' + "x" * 400
    done = subprocess.run(
        ["bash", "-c", f'. "{lib}"; json_str "$1"', "bash", text],
        capture_output=True,
        text=True,
        check=True,
    )
    decoded = json.loads(done.stdout)
    assert decoded.startswith('said "no"') and len(decoded) == 300
