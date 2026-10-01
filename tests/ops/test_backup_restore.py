"""scripts/backup.sh and scripts/restore.sh against a real PostgreSQL and (moto) S3.

The scripts need pg_dump, pg_restore, psql and rclone; the tests skip when one is missing.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts" / "backup.sh"
RESTORE = ROOT / "scripts" / "restore.sh"

pytestmark = pytest.mark.skipif(
    any(shutil.which(c) is None for c in ("pg_dump", "pg_restore", "psql", "rclone", "openssl")),
    reason="needs pg_dump, pg_restore, psql, rclone and openssl",
)

SCHEMA_WITHOUT_SETTING = """
CREATE TABLE alembic_version (version_num text PRIMARY KEY);
INSERT INTO alembic_version VALUES ('0003');
CREATE TABLE source (id serial PRIMARY KEY, name text);
CREATE TABLE evidence_document (id serial PRIMARY KEY, storage_key text);
CREATE TABLE measurement (id serial PRIMARY KEY);
CREATE TABLE assessment_version (id serial PRIMARY KEY);
CREATE TABLE app_user (id serial PRIMARY KEY, email text);
CREATE TABLE job (id serial PRIMARY KEY);
INSERT INTO source (name) VALUES ('nbs'), ('nerc');
INSERT INTO app_user (email) VALUES ('a@example.org');
INSERT INTO evidence_document (storage_key) VALUES ('evidence/ab/cd/one.html'), ('evidence/ef/01/two.pdf');
"""

SETTING = """
CREATE TABLE setting (key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
"""

SCHEMA = SCHEMA_WITHOUT_SETTING + SETTING


def libpq(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def with_db(url: str, name: str) -> str:
    return re.sub(r"/[^/?]+(\?|$)", f"/{name}\\1", libpq(url), count=1)


def psql(url: str, sql: str) -> str:
    done = subprocess.run(
        ["psql", url, "-v", "ON_ERROR_STOP=1", "-Atq", "-c", sql],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture(scope="module")
def admin_url() -> str:
    url = libpq(os.environ["DATABASE_URL"])
    try:
        psql(url, "SELECT 1")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("PostgreSQL is not reachable")
    return url


@pytest.fixture
def scratch_db(admin_url: str) -> Iterator[str]:
    """A populated source database; yields its URL. Also drops the databases tests restore into."""
    suffix = uuid.uuid4().hex[:8]
    created = []

    def make(name: str) -> str:
        db = f"ops_{name}_{suffix}"
        psql(admin_url, f'CREATE DATABASE "{db}"')
        created.append(db)
        return with_db(admin_url, db)

    src = make("src")
    psql(src, SCHEMA)
    yield src
    # Databases the scripts create (see sibling_url) are named after the source's suffix.
    for db in created + [f"ops_restored_{suffix}", f"ops_fresh_{suffix}"]:
        psql(admin_url, f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


@pytest.fixture
def s3_server() -> Iterator[tuple[str, str, Any]]:
    """A local S3 server with buckets app, backups and drill; yields (endpoint, host, boto client)."""
    boto3 = pytest.importorskip("boto3")
    server_mod = pytest.importorskip("moto.server")
    server = server_mod.ThreadedMotoServer(port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id="k",
        aws_secret_access_key="s",
        region_name="us-east-1",
    )
    for bucket in ("app", "backups", "drill"):
        client.create_bucket(Bucket=bucket)
    try:
        yield endpoint, host, client
    finally:
        server.stop()


def run(script: Path, *args: str, env: dict[str, str] | None = None):
    full = {k: v for k, v in os.environ.items() if not k.startswith(("BACKUP_", "RESTORE_", "S3_"))}
    full.pop("AWS_CA_BUNDLE", None)  # rclone cannot load it; tests only talk to localhost
    full.pop("ENV", None)
    full["OPS_USE_CONSOLE_SETTINGS"] = "0"  # tests that exercise the console turn this back on
    full.update(env or {})
    return subprocess.run([str(script), *args], capture_output=True, text=True, env=full)


def dumps(backup_dir: Path) -> list[Path]:
    """The dump files (not their .sha256 sidecars) in a backup directory, oldest first."""
    return sorted(p for p in (backup_dir / "db").glob("africasignal-*") if p.suffix != ".sha256")


def sibling_url(scratch_db: str, role: str) -> str:
    """URL of a not-yet-existing database that the fixture will drop after the test."""
    return scratch_db.replace("/ops_src_", f"/ops_{role}_")


def test_backup_then_restore_round_trips(tmp_path: Path, admin_url: str, scratch_db: str) -> None:
    env = {"DATABASE_URL": scratch_db, "BACKUP_DIR": str(tmp_path)}
    done = run(BACKUP, env=env)
    assert done.returncode == 0, done.stderr
    [dump] = dumps(tmp_path)
    assert (tmp_path / "db" / f"{dump.name}.sha256").is_file()

    target = sibling_url(scratch_db, "restored")
    done = run(RESTORE, "--target-url", target, "--recreate", env={"BACKUP_DIR": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert psql(target, "SELECT count(*) FROM source") == "2"
    assert psql(target, "SELECT email FROM app_user") == "a@example.org"
    assert psql(target, "SELECT version_num FROM alembic_version") == "0003"


def test_sqlalchemy_style_database_url_is_accepted(tmp_path: Path, scratch_db: str) -> None:
    url = scratch_db.replace("postgresql://", "postgresql+psycopg://", 1)
    done = run(BACKUP, env={"DATABASE_URL": url, "BACKUP_DIR": str(tmp_path)})
    assert done.returncode == 0, done.stderr


def test_restore_picks_the_newest_dump_and_lists_them(
    tmp_path: Path, admin_url: str, scratch_db: str
) -> None:
    env = {"DATABASE_URL": scratch_db, "BACKUP_DIR": str(tmp_path)}
    assert run(BACKUP, env=env).returncode == 0
    psql(scratch_db, "INSERT INTO source (name) VALUES ('later')")
    time.sleep(1.1)  # dump names carry a one-second timestamp
    assert run(BACKUP, env=env).returncode == 0
    assert len(dumps(tmp_path)) == 2

    listing = run(RESTORE, "--list", env={"BACKUP_DIR": str(tmp_path)})
    assert listing.returncode == 0 and listing.stdout.count("africasignal-") == 2

    target = sibling_url(scratch_db, "restored")
    done = run(RESTORE, "--target-url", target, "--recreate", env={"BACKUP_DIR": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert psql(target, "SELECT count(*) FROM source") == "3"


def test_old_dumps_are_pruned_but_the_new_one_stays(tmp_path: Path, scratch_db: str) -> None:
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    old = db_dir / "africasignal-20200101T000000Z.dump"
    old.write_bytes(b"old")
    old_sum = db_dir / f"{old.name}.sha256"
    old_sum.write_text("x  old\n")
    forty_days_ago = time.time() - 40 * 86400
    for p in (old, old_sum):
        os.utime(p, (forty_days_ago, forty_days_ago))
    young = db_dir / "africasignal-20990101T000000Z.dump"
    young.write_bytes(b"young")

    env = {"DATABASE_URL": scratch_db, "BACKUP_DIR": str(tmp_path), "BACKUP_RETAIN_DAYS": "30"}
    done = run(BACKUP, env=env)
    assert done.returncode == 0, done.stderr
    assert not old.exists() and not old_sum.exists()
    assert young.exists()
    assert len(dumps(tmp_path)) == 2  # young + the new one


def test_failed_dump_fails_loudly_and_uploads_nothing(tmp_path: Path, admin_url: str) -> None:
    done = run(
        BACKUP,
        env={"DATABASE_URL": with_db(admin_url, "does_not_exist"), "BACKUP_DIR": str(tmp_path)},
    )
    assert done.returncode != 0
    assert not (tmp_path / "db").exists()


def test_heartbeat_is_pinged_on_success_and_on_failure(
    tmp_path: Path, admin_url: str, scratch_db: str
) -> None:
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        hb = f"http://127.0.0.1:{server.server_port}/ping/abc"
        env = {"BACKUP_DIR": str(tmp_path), "BACKUP_HEARTBEAT_URL": hb, "no_proxy": "127.0.0.1"}
        assert run(BACKUP, env={**env, "DATABASE_URL": scratch_db}).returncode == 0
        bad = with_db(admin_url, "does_not_exist")
        assert run(BACKUP, env={**env, "DATABASE_URL": bad}).returncode != 0
    finally:
        server.shutdown()
    assert seen == ["/ping/abc/start", "/ping/abc", "/ping/abc/start", "/ping/abc/fail"]


def test_restore_has_no_default_target(tmp_path: Path, scratch_db: str) -> None:
    assert (
        run(BACKUP, env={"DATABASE_URL": scratch_db, "BACKUP_DIR": str(tmp_path)}).returncode == 0
    )
    done = run(RESTORE, env={"BACKUP_DIR": str(tmp_path), "DATABASE_URL": scratch_db})
    assert done.returncode != 0 and "no target" in done.stderr


def test_restore_refuses_the_live_database(tmp_path: Path, scratch_db: str) -> None:
    env = {"DATABASE_URL": scratch_db, "BACKUP_DIR": str(tmp_path)}
    assert run(BACKUP, env=env).returncode == 0
    done = run(RESTORE, "--target-url", scratch_db, "--recreate", env=env)
    assert done.returncode != 0 and "same database as DATABASE_URL" in done.stderr
    assert psql(scratch_db, "SELECT count(*) FROM source") == "2"  # untouched


def test_restore_refuses_production_without_the_flag(
    tmp_path: Path, admin_url: str, scratch_db: str
) -> None:
    env = {"DATABASE_URL": scratch_db, "BACKUP_DIR": str(tmp_path)}
    assert run(BACKUP, env=env).returncode == 0
    target = sibling_url(scratch_db, "restored")
    done = run(RESTORE, "--target-url", target, "--recreate", env={**env, "ENV": "production"})
    assert done.returncode != 0 and "--overwrite-live" in done.stderr


def test_restore_refuses_a_database_that_already_has_tables(
    tmp_path: Path, admin_url: str, scratch_db: str
) -> None:
    env = {"DATABASE_URL": scratch_db, "BACKUP_DIR": str(tmp_path)}
    assert run(BACKUP, env=env).returncode == 0
    other = sibling_url(scratch_db, "fresh")
    psql(admin_url, f'CREATE DATABASE "{other.rsplit("/", 1)[1]}"')
    psql(other, "CREATE TABLE precious (id int)")
    done = run(RESTORE, "--target-url", other, env={"BACKUP_DIR": str(tmp_path)})
    assert done.returncode != 0 and "already has 1 table" in done.stderr
    assert psql(other, "SELECT count(*) FROM precious") == "0"


def test_restore_rejects_a_tampered_dump(tmp_path: Path, admin_url: str, scratch_db: str) -> None:
    env = {"DATABASE_URL": scratch_db, "BACKUP_DIR": str(tmp_path)}
    assert run(BACKUP, env=env).returncode == 0
    [dump] = dumps(tmp_path)
    with dump.open("ab") as fh:
        fh.write(b"tamper")
    done = run(
        RESTORE,
        "--target-url",
        sibling_url(scratch_db, "restored"),
        "--recreate",
        env={"BACKUP_DIR": str(tmp_path)},
    )
    assert done.returncode != 0 and "checksum mismatch" in done.stderr


def test_encrypted_backup_round_trips_and_needs_the_passphrase(
    tmp_path: Path, admin_url: str, scratch_db: str
) -> None:
    good = tmp_path / "pass"
    good.write_text("correct horse battery staple\n")
    wrong = tmp_path / "wrong"
    wrong.write_text("tr0ub4dor\n")
    store = tmp_path / "store"
    env = {
        "DATABASE_URL": scratch_db,
        "BACKUP_DIR": str(store),
        "BACKUP_PASSPHRASE_FILE": str(good),
    }
    assert run(BACKUP, env=env).returncode == 0
    [dump] = dumps(store)
    assert dump.name.endswith(".dump.enc")
    assert b"PGDMP" not in dump.read_bytes()[:16]

    target = sibling_url(scratch_db, "restored")
    args = ("--target-url", target, "--recreate")
    no_pass = run(RESTORE, *args, env={"BACKUP_DIR": str(store)})
    assert no_pass.returncode != 0 and "BACKUP_PASSPHRASE_FILE" in no_pass.stderr
    bad = run(RESTORE, *args, env={"BACKUP_DIR": str(store), "BACKUP_PASSPHRASE_FILE": str(wrong)})
    assert bad.returncode != 0
    ok = run(RESTORE, *args, env={"BACKUP_DIR": str(store), "BACKUP_PASSPHRASE_FILE": str(good)})
    assert ok.returncode == 0, ok.stderr
    assert psql(target, "SELECT count(*) FROM source") == "2"


def test_passphrase_can_be_given_as_a_variable(
    tmp_path: Path, admin_url: str, scratch_db: str
) -> None:
    env = {
        "DATABASE_URL": scratch_db,
        "BACKUP_DIR": str(tmp_path),
        "BACKUP_PASSPHRASE": "from the environment",
    }
    assert run(BACKUP, env=env).returncode == 0
    assert dumps(tmp_path)[0].name.endswith(".dump.enc")
    target = sibling_url(scratch_db, "restored")
    done = run(
        RESTORE,
        "--target-url",
        target,
        "--recreate",
        env={"BACKUP_DIR": str(tmp_path), "BACKUP_PASSPHRASE": "from the environment"},
    )
    assert done.returncode == 0, done.stderr


def test_s3_backup_copies_objects_and_restore_verifies_them(
    tmp_path: Path, admin_url: str, scratch_db: str, s3_server: tuple[str, str, Any]
) -> None:
    endpoint, host, s3 = s3_server
    s3.put_object(Bucket="app", Key="evidence/ab/cd/one.html", Body=b"one")
    s3.put_object(Bucket="app", Key="evidence/ef/01/two.pdf", Body=b"two")
    creds = {"_ACCESS_KEY_ID": "k", "_SECRET_ACCESS_KEY": "s"}
    env = {
        "DATABASE_URL": scratch_db,
        "ENV": "staging",
        "BACKUP_PASSPHRASE": "a staging passphrase",  # required outside development
        "no_proxy": host,
        "S3_ENDPOINT_URL": endpoint,
        "S3_BUCKET": "app",
        **{f"S3{k}": v for k, v in creds.items()},
        "BACKUP_S3_ENDPOINT_URL": endpoint,
        "BACKUP_S3_BUCKET": "backups",
        **{f"BACKUP_S3{k}": v for k, v in creds.items()},
    }
    done = run(BACKUP, env=env)
    assert done.returncode == 0, done.stderr
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket="backups")["Contents"]}
    assert any(k.startswith("africasignal/staging/db/africasignal-") for k in keys)
    assert "africasignal/staging/objects/evidence/ab/cd/one.html" in keys

    # Running again must not trip the immutability check on objects already copied.
    assert run(BACKUP, env=env).returncode == 0

    restore_env = {
        **{k: v for k, v in env.items() if k.startswith("BACKUP_") or k == "no_proxy"},
        "BACKUP_S3_PREFIX": "africasignal/staging",
        "RESTORE_S3_ENDPOINT_URL": endpoint,
        "RESTORE_S3_BUCKET": "drill",
        **{f"RESTORE_S3{k}": v for k, v in creds.items()},
    }
    target = sibling_url(scratch_db, "restored")
    done = run(
        RESTORE,
        "--target-url",
        target,
        "--recreate",
        "--restore-objects",
        "--verify-objects",
        "5",
        env=restore_env,
    )
    assert done.returncode == 0, done.stderr
    assert "all present" in done.stderr
    assert s3.get_object(Bucket="drill", Key="evidence/ef/01/two.pdf")["Body"].read() == b"two"

    # An object that the database points at but the bucket lacks is reported, not ignored.
    s3.delete_object(Bucket="drill", Key="evidence/ef/01/two.pdf")
    done = run(
        RESTORE,
        "--target-url",
        target,
        "--recreate",
        "--verify-objects",
        "5",
        env=restore_env,
    )
    assert done.returncode != 0 and "MISSING object: evidence/ef/01/two.pdf" in done.stderr

    # The backup bucket must not be the app bucket.
    same = {**env, "BACKUP_S3_BUCKET": "app"}
    done = run(BACKUP, env=same)
    assert done.returncode != 0 and "app's own bucket" in done.stderr


def prepare_console_database(url: str, settings: dict[str, str]) -> None:
    """Migrate ``url`` to head and save ``settings`` in it as an operator would in the console."""
    env = {**os.environ, "DATABASE_URL": url.replace("postgresql://", "postgresql+psycopg://", 1)}
    env["SECRET_KEY"] = "console-secret-key-for-tests"
    env.pop("ENV", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    code = (
        "import json, sys\n"
        "from africasignal import operators, settings_store\n"
        "from africasignal.db import session_scope\n"
        "with session_scope() as session:\n"
        "    op, _ = operators.create_operator(session, 'ops@example.org', 'a long enough passphrase 42', 'admin')\n"
        "    for key, value in json.loads(sys.argv[1]).items():\n"
        "        settings_store.set_value(session, op, key, value)\n"
    )
    subprocess.run(
        [sys.executable, "-c", code, json.dumps(settings)],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )


def test_console_settings_choose_the_buckets_and_beat_the_environment(
    tmp_path: Path, admin_url: str, scratch_db: str, s3_server: tuple[str, str, Any]
) -> None:
    endpoint, host, s3 = s3_server
    s3.put_object(Bucket="app", Key="evidence/ab/cd/one.html", Body=b"one")
    live = sibling_url(scratch_db, "fresh")
    psql(admin_url, f'CREATE DATABASE "{live.rsplit("/", 1)[1]}"')
    prepare_console_database(
        live,
        {
            "backup_s3_endpoint_url": endpoint,
            "backup_s3_bucket": "backups",
            "backup_s3_access_key_id": "k-console",
            "backup_s3_secret_access_key": "s-console-secret",
            "backup_s3_prefix": "console/prefix",
            "backup_retain_days": "7",
            "s3_endpoint_url": endpoint,
            "s3_bucket": "app",
            "s3_access_key_id": "k-console",
            "s3_secret_access_key": "s-console-secret",
        },
    )
    env = {
        "DATABASE_URL": live.replace("postgresql://", "postgresql+psycopg://", 1),
        "SECRET_KEY": "console-secret-key-for-tests",
        "OPS_USE_CONSOLE_SETTINGS": "1",
        "OPS_PYTHON": sys.executable,
        "no_proxy": host,
        # Wrong values in the environment: the console must win.
        "BACKUP_S3_ENDPOINT_URL": "http://127.0.0.1:1",
        "BACKUP_S3_BUCKET": "not-the-console-bucket",
        "BACKUP_S3_ACCESS_KEY_ID": "wrong",
        "BACKUP_S3_SECRET_ACCESS_KEY": "wrong",
        "BACKUP_RETAIN_DAYS": "999",
    }
    # moto accepts any credentials, so the access key used is proven by the prefix and bucket below.
    done = run(BACKUP, env=env)
    assert done.returncode == 0, done.stderr
    assert "pruning dumps older than 7 days" in done.stderr
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket="backups")["Contents"]}
    assert any(k.startswith("console/prefix/db/africasignal-") for k in keys)
    assert "console/prefix/objects/evidence/ab/cd/one.html" in keys

    listing = run(RESTORE, "--list", env=env)
    assert listing.returncode == 0 and "africasignal-" in listing.stdout
    target = sibling_url(scratch_db, "restored")
    done = run(RESTORE, "--target-url", target, "--recreate", env=env)
    assert done.returncode == 0, done.stderr
    assert psql(target, "SELECT count(*) FROM operator") == "1"


def test_environment_is_used_when_the_console_cannot_be_read(
    tmp_path: Path, scratch_db: str
) -> None:
    # Exercise the legacy/missing-settings fallback explicitly; current schemas have setting.
    psql(scratch_db, "DROP TABLE setting")
    env = {
        "DATABASE_URL": scratch_db.replace("postgresql://", "postgresql+psycopg://", 1),
        "BACKUP_DIR": str(tmp_path),
        "OPS_USE_CONSOLE_SETTINGS": "1",
        "OPS_PYTHON": sys.executable,
        "BACKUP_RETAIN_DAYS": "12",
    }
    done = run(BACKUP, env=env)
    assert done.returncode == 0, done.stderr
    assert "console settings could not be read" in done.stderr
    assert "pruning dumps older than 12 days" in done.stderr


# --- security review S-15 ----------------------------------------------------------------------


def test_outside_development_a_backup_must_be_encrypted(tmp_path: Path, scratch_db: str) -> None:
    env = {"DATABASE_URL": scratch_db, "BACKUP_DIR": str(tmp_path), "ENV": "production"}
    done = run(BACKUP, env=env)
    assert done.returncode != 0 and "dumps must be encrypted" in done.stderr
    assert dumps(tmp_path) == []
    done = run(BACKUP, env={**env, "BACKUP_PASSPHRASE": "from the environment"})
    assert done.returncode == 0, done.stderr
    [dump] = dumps(tmp_path)
    assert dump.name.endswith(".dump.enc")


def test_a_restore_without_a_checksum_needs_no_verify(tmp_path: Path, scratch_db: str) -> None:
    assert (
        run(BACKUP, env={"DATABASE_URL": scratch_db, "BACKUP_DIR": str(tmp_path)}).returncode == 0
    )
    [dump] = dumps(tmp_path)
    (tmp_path / "db" / f"{dump.name}.sha256").unlink()
    target = sibling_url(scratch_db, "restored")
    args = ("--target-url", target, "--recreate")
    refused = run(RESTORE, *args, env={"BACKUP_DIR": str(tmp_path)})
    assert refused.returncode != 0 and "cannot be checked" in refused.stderr
    assert "--no-verify" in refused.stderr
    done = run(RESTORE, *args, "--no-verify", env={"BACKUP_DIR": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert psql(target, "SELECT count(*) FROM source") == "2"


def _spy_on_postgres_tools(bin_dir: Path) -> Path:
    """Wrappers for pg_dump, pg_restore and psql that record their arguments, then run the real
    tool. Returns the log file."""
    bin_dir.mkdir()
    log = bin_dir / "calls.log"
    for tool in ("pg_dump", "pg_restore", "psql"):
        real = shutil.which(tool)
        wrapper = bin_dir / tool
        wrapper.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexec "{real}" "$@"\n')
        wrapper.chmod(0o755)
    return log


def test_the_database_password_is_not_on_any_command_line(
    tmp_path: Path, admin_url: str, scratch_db: str
) -> None:
    """A password with characters that need quoting, so the pgpass escaping is exercised too."""
    role = f"ops_pw_{uuid.uuid4().hex[:8]}"
    password = "p:a@s/s w%rd\\"
    quoted = password.replace("'", "''")
    psql(admin_url, f"CREATE ROLE {role} SUPERUSER LOGIN PASSWORD '{quoted}'")
    try:
        from urllib.parse import quote, urlsplit  # noqa: PLC0415

        parts = urlsplit(scratch_db)
        url = (
            f"postgresql://{role}:{quote(password, safe='')}@{parts.hostname}:{parts.port or 5432}"
            f"{parts.path}"
        )
        bin_dir = tmp_path / "bin"
        log = _spy_on_postgres_tools(bin_dir)
        path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        store = tmp_path / "store"
        done = run(BACKUP, env={"DATABASE_URL": url, "BACKUP_DIR": str(store), "PATH": path})
        assert done.returncode == 0, done.stderr
        target = sibling_url(scratch_db, "restored")
        target_url = url.replace(parts.path.lstrip("/"), target.rsplit("/", 1)[1])
        done = run(
            RESTORE,
            "--target-url",
            target_url,
            "--recreate",
            "--overwrite-live",
            env={"BACKUP_DIR": str(store), "DATABASE_URL": url, "PATH": path},
        )
        assert done.returncode == 0, done.stderr
        assert psql(target, "SELECT count(*) FROM source") == "2"
        calls = log.read_text()
        assert role in calls  # the user name is still passed
        for secret in (password, quote(password, safe=""), "s w%rd", "s%2Fs"):
            assert secret not in calls, calls
    finally:
        restored = sibling_url(scratch_db, "restored").rsplit("/", 1)[1]  # the role owns it now
        psql(admin_url, f'DROP DATABASE IF EXISTS "{restored}" WITH (FORCE)')
        psql(admin_url, f"DROP ROLE IF EXISTS {role}")
