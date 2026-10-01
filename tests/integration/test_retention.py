# ruff: noqa: F811
"""Retention and the deletion ledger (AS-043 gaps G1, G2, G3, G4): the daily clean-up deletes what
the privacy notice says it deletes, and a restored backup does not bring deleted accounts back."""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from africasignal import admin as admin_cli
from africasignal import settings_store as ss
from africasignal.jobs import handlers, queue
from africasignal.jobs.handlers import JobContext
from africasignal.models import (
    AccountDeletion,
    AppUser,
    Event,
    Feedback,
    Job,
    LoginToken,
    Place,
    UserSession,
)
from africasignal.publish import deletions, retention
from africasignal.publish.accounts import delete_account, export_account
from africasignal.storage import S3Store
from tests.integration.email_support import add_place, add_situation, add_user, add_version
from tests.integration.nbs_support import make_store
from tests.integration.test_admin_console import make_operator

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from make_store()


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for d in ss.REGISTRY.values():
        monkeypatch.delenv(d.env_name, raising=False)


@pytest.fixture
def job_handlers(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Import the retention handlers into a private registry. A global registration would make
    other tests' schedulers enqueue their jobs, and the module must be importable again afterwards
    so ``load_all`` registers it for real."""
    name = "africasignal.jobs.handlers.apply_retention"
    monkeypatch.setattr(handlers, "HANDLERS", {})
    original = sys.modules.pop(name, None)
    module = importlib.import_module(name)
    yield module
    # Put back whatever was there: when an earlier test already ran ``load_all``, the real
    # registry holds this module's handlers, and importing it again would register them twice.
    sys.modules.pop(name, None)
    if original is None:
        if hasattr(handlers, "apply_retention"):
            delattr(handlers, "apply_retention")
    else:
        sys.modules[name] = original
        handlers.apply_retention = original  # type: ignore[attr-defined]


def count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def aged_user(session: Session, email: str, age: timedelta, verified: bool = True) -> AppUser:
    user = add_user(session, email, verified=verified)
    user.created_at = NOW - age
    if verified:
        user.email_verified_at = NOW - age
    session.flush()
    return user


# --- login tokens and sessions (G1) ----------------------------------------------------------


def test_login_tokens_and_sessions_go_thirty_days_after_they_end(session: Session) -> None:
    user = aged_user(session, "a@example.org", 100 * DAY)
    for end in (NOW - 31 * DAY, NOW - 29 * DAY):  # expired: long ago / recently
        session.add(LoginToken(user_id=user.id, token_sha256="t", expires_at=end))
        session.add(UserSession(user_id=user.id, token_sha256="s", expires_at=end))
    session.add(  # still valid, never used
        LoginToken(user_id=user.id, token_sha256="t", expires_at=NOW + DAY)
    )
    session.add(  # signed out long ago, though its natural expiry is ahead
        UserSession(
            user_id=user.id, token_sha256="s", expires_at=NOW + DAY, revoked_at=NOW - 40 * DAY
        )
    )
    session.add(UserSession(user_id=user.id, token_sha256="s", expires_at=NOW + 20 * DAY))
    session.flush()

    assert retention.purge_login_records(session, NOW) == (1, 2)
    assert count(session, LoginToken) == 2
    assert count(session, UserSession) == 2
    assert retention.purge_login_records(session, NOW) == (0, 0)  # nothing more to do


# --- accounts that were never verified (G1) --------------------------------------------------


def test_an_account_never_verified_is_deleted_after_thirty_days(session: Session) -> None:
    stale = aged_user(session, "stale@example.org", 31 * DAY, verified=False)
    session.add(LoginToken(user_id=stale.id, token_sha256="t", expires_at=NOW - 31 * DAY))
    fresh = aged_user(session, "fresh@example.org", 5 * DAY, verified=False)
    verified = aged_user(session, "member@example.org", 400 * DAY)
    session.flush()

    assert retention.purge_unverified_accounts(session, NOW) == 1

    emails = set(session.scalars(select(AppUser.email)))
    assert emails == {fresh.email, verified.email}
    assert count(session, LoginToken) == 0
    assert count(session, AccountDeletion) == 0  # no ledger entry: the job would repeat it


# --- feedback (G2) ---------------------------------------------------------------------------


def _feedback(session: Session, age: timedelta, user_id: int | None = None) -> Feedback:
    lagos = session.scalars(select(Place).where(Place.code == "NG-LA")).first() or add_place(
        session, "NG-LA", "Lagos", "state"
    )
    version = add_version(session, add_situation(session, f"s-{age.days}-{user_id}", lagos))
    row = Feedback(
        assessment_version_id=version.id,
        user_id=user_id,
        anon_id="visitor-code-0123456789",
        kind="error_report",
        text="the price is wrong, call me on 0803",
        contact_email="reader@example.org",
        status="resolved_updated",
        resolution_note="Corrected in version 3.",
    )
    session.add(row)
    session.flush()
    row.created_at = NOW - age
    session.flush()
    return row


def test_feedback_loses_its_personal_content_after_the_retention_period(
    session: Session,
) -> None:
    user = aged_user(session, "a@example.org", 900 * DAY)
    old = _feedback(session, 800 * DAY, user.id)
    recent = _feedback(session, 100 * DAY)

    assert retention.feedback_retention_months(session) == 24  # the default
    assert retention.scrub_old_feedback(session, NOW, 24) == 1

    session.refresh(old)
    session.refresh(recent)
    assert (old.text, old.contact_email, old.anon_id, old.user_id) == (None, None, None, None)
    # what was corrected, and the vote, stay
    assert old.kind == "error_report" and old.status == "resolved_updated"
    assert old.resolution_note == "Corrected in version 3."
    assert recent.text and recent.contact_email and recent.anon_id
    assert retention.scrub_old_feedback(session, NOW, 24) == 0  # repeating changes nothing


def test_the_feedback_period_is_a_console_setting(session: Session) -> None:
    operator = make_operator(session).operator
    row = _feedback(session, 100 * DAY)
    ss.set_value(session, operator, "feedback_retention_months", "3")
    assert retention.feedback_retention_months(session) == 3
    assert retention.run(session, NOW, None).feedback_scrubbed == 1
    session.refresh(row)
    assert row.text is None

    with pytest.raises(ss.SettingError):
        ss.set_value(session, operator, "feedback_retention_months", "0")  # never "delete all"
    assert "feedback_retention_months" in ss.group_keys("privacy")


# --- the deletion ledger ---------------------------------------------------------------------


def test_a_deletion_is_recorded_without_the_address_and_queues_a_copy(session: Session) -> None:
    user = aged_user(session, "Ada@Example.org", 50 * DAY)
    assert delete_account(session, user.id, NOW)

    row = session.scalars(select(AccountDeletion)).one()
    assert row.email_hmac == deletions.fingerprint("ada@example.org")  # case-insensitive
    assert row.deleted_at == NOW and row.mirrored_at is None
    assert len(row.email_hmac) == 64 and "ada" not in row.email_hmac
    assert session.scalar(
        select(func.count()).select_from(Job).where(Job.kind == deletions.MIRROR_JOB)
    )
    # the address is nowhere in the database any more, the ledger included
    assert (
        session.execute(
            text("SELECT count(*) FROM account_deletion WHERE email_hmac ILIKE '%ada@%'")
        ).scalar_one()
        == 0
    )


def test_the_fingerprint_is_keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    first = deletions.fingerprint("ada@example.org")
    monkeypatch.setenv("SECRET_KEY", "a-different-secret-key-for-this-test-0123456789")
    from africasignal.config import get_settings

    get_settings.cache_clear()
    try:
        assert deletions.fingerprint("ada@example.org") != first
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_the_copy_to_object_storage_is_made_once(session: Session, store: S3Store) -> None:
    user = aged_user(session, "ada@example.org", 50 * DAY)
    delete_account(session, user.id, NOW)
    assert deletions.mirror_pending(session, None, NOW) == 0  # no bucket: stays pending
    assert deletions.mirror_pending(session, store, NOW) == 1
    assert deletions.mirror_pending(session, store, NOW) == 0

    (key,) = store.list_keys(deletions.LEDGER_PREFIX)
    assert key == f"deletions/2026-10-01/{deletions.fingerprint('ada@example.org')}.json"
    body = json.loads(store.get(key))
    assert set(body) == {"email_hmac", "deleted_at"}
    assert b"ada@example.org" not in store.get(key)


def test_the_mirror_job_copies_pending_entries(
    session: Session, store: S3Store, monkeypatch: pytest.MonkeyPatch, job_handlers: object
) -> None:
    user = aged_user(session, "ada@example.org", 50 * DAY)
    delete_account(session, user.id, NOW)
    job_id = session.scalar(select(Job.id).where(Job.kind == deletions.MIRROR_JOB))
    ctx = JobContext(
        session=session,
        job=queue.ClaimedJob(
            id=job_id, kind=deletions.MIRROR_JOB, payload={}, attempts=1, max_attempts=5
        ),
        worker_id="test",
    )
    monkeypatch.setattr(job_handlers, "store_for_session", lambda s: None)
    handlers.get_handler(deletions.MIRROR_JOB)(ctx)  # type: ignore[misc]
    assert store.list_keys(deletions.LEDGER_PREFIX) == []  # nothing configured: no crash

    monkeypatch.setattr(job_handlers, "store_for_session", lambda s: store)
    handlers.get_handler(deletions.MIRROR_JOB)(ctx)  # type: ignore[misc]
    assert len(store.list_keys(deletions.LEDGER_PREFIX)) == 1


# --- re-applying deletions after a restore (G4) ----------------------------------------------


def _restore_from_backup(session: Session, *addresses: str, created: datetime) -> None:
    """What restoring an earlier dump does: the accounts are back and the ledger table holds
    only what it held then (nothing)."""
    session.execute(text("DELETE FROM account_deletion"))
    for email in addresses:
        add_user(session, email).created_at = created
    session.flush()


def test_a_restored_backup_does_not_bring_deleted_accounts_back(
    session: Session, store: S3Store
) -> None:
    dump_time = NOW - 3 * DAY
    leaver = aged_user(session, "leaver@example.org", 50 * DAY)
    delete_account(session, leaver.id, NOW - DAY)
    deletions.mirror_pending(session, store, NOW - DAY)

    _restore_from_backup(session, "leaver@example.org", "stayer@example.org", created=dump_time)
    assert count(session, AccountDeletion) == 0

    assert retention.reapply_deletions(session, NOW, store) == 1

    assert set(session.scalars(select(AppUser.email))) == {"stayer@example.org"}
    # the entry is back on the books (and stays copied), so a second restore is covered too
    row = session.scalars(select(AccountDeletion)).one()
    assert row.email_hmac == deletions.fingerprint("leaver@example.org")
    assert row.mirrored_at == NOW
    assert retention.reapply_deletions(session, NOW, store) == 0  # safe to repeat


def test_a_new_sign_up_after_the_deletion_is_not_deleted(session: Session, store: S3Store) -> None:
    leaver = aged_user(session, "back@example.org", 50 * DAY)
    delete_account(session, leaver.id, NOW - 2 * DAY)
    deletions.mirror_pending(session, store, NOW - 2 * DAY)
    add_user(session, "back@example.org").created_at = NOW - DAY  # signed up again later

    assert retention.reapply_deletions(session, NOW, store) == 0
    assert set(session.scalars(select(AppUser.email))) == {"back@example.org"}


def test_the_table_alone_is_enough_when_no_store_is_configured(session: Session) -> None:
    leaver = aged_user(session, "leaver@example.org", 50 * DAY)
    delete_account(session, leaver.id, NOW - DAY)
    add_user(session, "leaver@example.org").created_at = NOW - 10 * DAY  # a restored copy
    assert retention.reapply_deletions(session, NOW, None) == 1
    assert count(session, AppUser) == 0


def test_reapplying_also_clears_what_the_account_left_behind(
    session: Session, store: S3Store
) -> None:
    user = aged_user(session, "leaver@example.org", 50 * DAY)
    delete_account(session, user.id, NOW - DAY)
    deletions.mirror_pending(session, store, NOW - DAY)
    session.execute(text("DELETE FROM account_deletion"))
    restored = add_user(session, "leaver@example.org")
    restored.created_at = NOW - 10 * DAY
    session.add(UserSession(user_id=restored.id, token_sha256="s", expires_at=NOW + DAY))
    session.flush()

    assert retention.reapply_deletions(session, NOW, store) == 1
    assert count(session, UserSession) == 0


def test_the_command_reapplies_deletions(
    session: Session,
    store: S3Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from contextlib import contextmanager

    @contextmanager
    def scope() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(admin_cli, "session_scope", scope)
    monkeypatch.setattr("africasignal.storage.store_for_session", lambda s: store)
    leaver = aged_user(session, "leaver@example.org", 50 * DAY)
    delete_account(session, leaver.id, NOW - DAY)
    deletions.mirror_pending(session, store, NOW - DAY)
    add_user(session, "leaver@example.org").created_at = NOW - 10 * DAY

    assert admin_cli.main(["reapply-deletions"]) == 0
    assert "Deleted 1 account(s)" in capsys.readouterr().out
    assert count(session, AppUser) == 0

    monkeypatch.setattr("africasignal.storage.store_for_session", lambda s: None)
    assert admin_cli.main(["reapply-deletions"]) == 0
    assert "no object storage is configured" in capsys.readouterr().err


# --- pruning and the daily job ---------------------------------------------------------------


def test_ledger_entries_are_dropped_once_no_backup_can_hold_the_account(
    session: Session, store: S3Store
) -> None:
    operator = make_operator(session).operator
    ss.set_value(session, operator, "backup_retain_days", "10")
    old = aged_user(session, "old@example.org", 90 * DAY)
    recent = aged_user(session, "recent@example.org", 90 * DAY)
    delete_account(session, old.id, NOW - 13 * DAY)  # 10 days of dumps + 2 days margin passed
    delete_account(session, recent.id, NOW - 11 * DAY)
    deletions.mirror_pending(session, store, NOW)
    assert len(store.list_keys(deletions.LEDGER_PREFIX)) == 2

    assert retention.ledger_keep(session) == timedelta(days=12)
    result = retention.run(session, NOW, store)

    assert result.ledger_pruned == 1
    assert [e.email_hmac for e in deletions.database_entries(session)] == [
        deletions.fingerprint("recent@example.org")
    ]
    assert [e.email_hmac for e in deletions.stored_entries(store)] == [
        deletions.fingerprint("recent@example.org")
    ]


def test_the_daily_job_runs_everything_and_reports_counts(session: Session, store: S3Store) -> None:
    aged_user(session, "stale@example.org", 40 * DAY, verified=False)
    member = aged_user(session, "member@example.org", 40 * DAY)
    session.add(UserSession(user_id=member.id, token_sha256="s", expires_at=NOW - 40 * DAY))
    _feedback(session, 800 * DAY)
    leaver = aged_user(session, "leaver@example.org", 40 * DAY)
    delete_account(session, leaver.id, NOW)
    session.flush()

    result = retention.run(session, NOW, store)

    assert (result.unverified_accounts, result.sessions, result.feedback_scrubbed) == (1, 1, 1)
    assert result.ledger_mirrored == 1
    assert set(session.scalars(select(AppUser.email))) == {"member@example.org"}


def test_the_job_is_registered_and_scheduled(session: Session, job_handlers: object) -> None:
    from africasignal.jobs import scheduler

    assert handlers.get_handler("apply_retention") is not None
    assert handlers.get_handler(deletions.MIRROR_JOB) is not None
    scheduler.tick(session, NOW)
    kinds = set(session.scalars(select(Job.kind)))
    assert "apply_retention" in kinds


# --- the export (G3) -------------------------------------------------------------------------


def test_the_export_includes_the_page_views_linked_to_the_account(session: Session) -> None:
    user = aged_user(session, "reader@example.org", 10 * DAY)
    other = aged_user(session, "other@example.org", 10 * DAY)
    situation = add_situation(session, "pms-ng-la", add_place(session, "NG-LA", "Lagos", "state"))
    session.add_all(
        [
            Event(
                user_id=user.id,
                anon_id="visitor-code-0123456789",
                name="situation_view",
                situation_id=situation.id,
                ref="wa",
                props={"page": "situation"},
                ts=NOW - DAY,
            ),
            Event(user_id=user.id, name="digest_open", ref="email", props={"week": "2026-W40"}),
            Event(user_id=other.id, name="page_view", props={"page": "home"}),
            Event(anon_id="visitor-code-0123456789", name="page_view"),  # before sign-in
        ]
    )
    session.flush()

    data = export_account(session, user.id)
    assert data is not None
    events = data["events"]
    assert len(events) == 2  # only this account's, none of the other user's or anonymous ones
    first = next(e for e in events if e["event"] == "situation_view")
    assert first["situation"] == "pms-ng-la"
    assert first["came_from"] == "wa"
    assert first["visitor_code"] == "visitor-code-0123456789"
    assert first["details"] == {"page": "situation"}
    json.dumps(data)  # still serialisable


def test_the_export_of_feedback_includes_what_is_stored_with_it(session: Session) -> None:
    user = aged_user(session, "reader@example.org", 10 * DAY)
    _feedback(session, DAY, user.id)
    (entry,) = export_account(session, user.id)["feedback"]  # type: ignore[index]
    assert entry["contact_email"] == "reader@example.org"
    assert entry["visitor_code"] == "visitor-code-0123456789"
