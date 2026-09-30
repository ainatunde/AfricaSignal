"""Operator console (AS-022): sign-in, CSRF, permission approval, audit, and the fetch gate."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal import admin as admin_cli
from africasignal import operators
from africasignal.jobs import queue
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.handlers import fetch_source as fetch_source_module
from africasignal.models import AuditLog, Job, Operator, Source, SourcePermission
from africasignal.sources.base import AdapterContext, DiscoveredItem, ProcessResult
from africasignal.sources.permissions import current_permission
from africasignal.web.app import create_app
from africasignal.web.deps import COOKIE_NAME, get_db

PASSWORD = "correct horse battery staple"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        yield c


class Account:
    def __init__(self, operator: Operator, secret: str) -> None:
        self.operator = operator
        self.secret = secret

    def code(self) -> str:
        return pyotp.TOTP(self.secret).now()


def make_operator(session: Session, email: str = "ops@example.org", role: str = "admin") -> Account:
    operator, secret = operators.create_operator(session, email, PASSWORD, role)
    return Account(operator, secret)


def sign_in(client: TestClient, account: Account, **override: str) -> object:
    form = {"email": account.operator.email, "password": PASSWORD, "code": account.code()}
    return client.post("/admin/login", data={**form, **override}, headers=ORIGIN)


def signed_in(client: TestClient, account: Account) -> TestClient:
    response = sign_in(client, account)
    assert response.status_code == 303  # type: ignore[attr-defined]
    return client


def make_source(session: Session, slug: str = "nbs-test", **perm: object) -> Source:
    source = Source(
        slug=slug,
        name=f"Source {slug}",
        kind="official_statistics",
        adapter="nbs",
        schedule_minutes=60,
    )
    session.add(source)
    session.flush()
    fields: dict[str, object] = {
        "version": 1,
        "may_collect": True,
        "may_store_full_text": True,
        "may_republish_numbers": True,
        "rights_basis": "Public government statistics",
        "terms_url": "https://example.org/terms",
    }
    fields.update(perm)
    session.add(SourcePermission(source_id=source.id, **fields))
    session.flush()
    return source


def audit_actions(session: Session) -> list[str]:
    return list(session.scalars(select(AuditLog.action).order_by(AuditLog.id)))


# --- sign-in ------------------------------------------------------------------------------------


def test_console_pages_redirect_to_sign_in_when_signed_out(client: TestClient) -> None:
    for path in ("/admin", "/admin/sources", "/admin/sources/1", "/admin/audit"):
        response = client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/admin/login"


def test_sign_in_with_password_and_code(client: TestClient, session: Session) -> None:
    account = make_operator(session)
    response = sign_in(client, account)
    assert response.status_code == 303  # type: ignore[attr-defined]
    assert response.headers["location"] == "/admin/sources"  # type: ignore[attr-defined]
    cookie = response.headers["set-cookie"].lower()  # type: ignore[attr-defined]
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/admin" in cookie
    page = client.get("/admin/sources")
    assert page.status_code == 200 and "ops@example.org" in page.text
    assert "sign_in" in audit_actions(session)[0]


def test_audit_page_shows_rows_the_system_wrote_as_system(
    client: TestClient, session: Session
) -> None:
    from africasignal import audit

    account = make_operator(session)
    signed_in(client, account)
    audit.record_system(session, "alert.opened", "alert", after={"code": "backup_stale"})
    page = client.get("/admin/audit")
    assert page.status_code == 200
    assert "alert.opened" in page.text and "system" in page.text


def test_wrong_totp_code_is_refused(client: TestClient, session: Session) -> None:
    account = make_operator(session)
    response = sign_in(client, account, code="000000" if account.code() != "000000" else "000001")
    assert response.status_code == 401  # type: ignore[attr-defined]
    assert COOKIE_NAME not in client.cookies
    assert client.get("/admin/sources").status_code == 303
    assert audit_actions(session) == ["operator.sign_in_failed"]


def test_wrong_password_is_refused_with_the_same_message_as_wrong_code(
    client: TestClient, session: Session
) -> None:
    account = make_operator(session)
    bad_password = sign_in(client, account, password="not the password at all")
    bad_code = sign_in(client, account, code="123456" if account.code() != "123456" else "654321")
    unknown = sign_in(client, account, email="nobody@example.org")
    messages = {
        re.search(r'role="alert">(.*?)</p>', r.text).group(1)  # type: ignore[attr-defined,union-attr]
        for r in (bad_password, bad_code, unknown)
    }
    assert len(messages) == 1
    assert all(r.status_code == 401 for r in (bad_password, bad_code, unknown))  # type: ignore[attr-defined]


def test_a_wrong_password_does_not_burn_the_operators_real_code(
    client: TestClient, session: Session
) -> None:
    account = make_operator(session)
    assert sign_in(client, account, password="not the password at all").status_code == 401  # type: ignore[attr-defined]
    assert sign_in(client, account).status_code == 303  # type: ignore[attr-defined]


def test_disabled_operator_cannot_sign_in(client: TestClient, session: Session) -> None:
    account = make_operator(session)
    account.operator.disabled_at = datetime.now(UTC)
    session.flush()
    response = sign_in(client, account)
    assert response.status_code == 401  # type: ignore[attr-defined]
    assert COOKIE_NAME not in client.cookies


def test_disabling_an_operator_ends_their_session(client: TestClient, session: Session) -> None:
    account = make_operator(session)
    signed_in(client, account)
    assert client.get("/admin/sources").status_code == 200
    account.operator.disabled_at = datetime.now(UTC)
    session.flush()
    assert client.get("/admin/sources").status_code == 303


def test_changing_the_password_ends_existing_sessions(client: TestClient, session: Session) -> None:
    account = make_operator(session)
    signed_in(client, account)
    account.operator.password_hash = operators.hash_password("a brand new long password")
    session.flush()
    assert client.get("/admin/sources").status_code == 303


def test_repeated_failures_lock_the_email_out_even_with_good_credentials(
    client: TestClient, session: Session
) -> None:
    account = make_operator(session)
    for _ in range(operators.CLIENT_MAX_FAILURES):
        assert sign_in(client, account, password="wrong password here").status_code == 401  # type: ignore[attr-defined]
    response = sign_in(client, account)
    assert response.status_code == 429  # type: ignore[attr-defined]
    assert COOKIE_NAME not in client.cookies


def test_a_tampered_cookie_is_refused(client: TestClient, session: Session) -> None:
    account = make_operator(session)
    signed_in(client, account)
    value = client.cookies[COOKIE_NAME]
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, value[:-3] + "AAA", path="/admin")
    assert client.get("/admin/sources").status_code == 303


def test_sign_out_clears_the_cookie(client: TestClient, session: Session) -> None:
    signed_in(client, make_operator(session))
    response = client.post("/admin/logout", headers=ORIGIN)
    assert response.status_code == 303
    assert client.get("/admin/sources").status_code == 303


def test_console_responses_carry_security_headers(client: TestClient) -> None:
    headers = client.get("/admin/login").headers
    assert headers["cache-control"] == "no-store"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


# --- CSRF ---------------------------------------------------------------------------------------


def test_post_without_origin_is_refused_before_anything_happens(
    client: TestClient, session: Session
) -> None:
    account = make_operator(session)
    form = {"email": account.operator.email, "password": PASSWORD, "code": account.code()}
    response = client.post("/admin/login", data=form)
    assert response.status_code == 403
    assert COOKIE_NAME not in client.cookies
    # the code was not consumed by the refused request
    assert client.post("/admin/login", data=form, headers=ORIGIN).status_code == 303


def test_cross_origin_post_to_a_console_action_is_refused(
    client: TestClient, session: Session
) -> None:
    source = make_source(session, approved_at=None)  # type: ignore[arg-type]
    signed_in(client, make_operator(session))
    url = f"/admin/sources/{source.id}/permissions/1/approve"
    for headers in ({"Origin": "https://evil.example.org"}, {"Origin": "null"}, {}):
        response = client.post(url, data={"terms_reviewed": "on"}, headers=headers)
        assert response.status_code == 403
    assert current_permission(session, source.id) is None
    assert audit_actions(session) == ["operator.sign_in"]


def test_get_requests_are_not_origin_checked(client: TestClient) -> None:
    assert (
        client.get("/admin/login", headers={"Origin": "https://evil.example.org"}).status_code
        == 200
    )


def test_trusted_origin_setting_is_honoured(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = make_operator(session)
    public = {"Origin": "https://console.example.org"}
    form = {"email": account.operator.email, "password": PASSWORD, "code": account.code()}
    assert client.post("/admin/login", data=form, headers=public).status_code == 403
    monkeypatch.setenv("ADMIN_TRUSTED_ORIGINS", "https://console.example.org")
    assert client.post("/admin/login", data=form, headers=public).status_code == 303


# --- sources page -------------------------------------------------------------------------------


def test_sources_page_says_which_sources_cannot_be_fetched(
    client: TestClient, session: Session
) -> None:
    make_source(session, "pending-one", approved_at=None)  # type: ignore[arg-type]
    make_source(session, "approved-one", approved_at=datetime.now(UTC))  # type: ignore[arg-type]
    signed_in(client, make_operator(session))
    page = client.get("/admin/sources").text
    assert "Awaiting approval" in page and "cannot be fetched" in page
    assert "Approved (v1)" in page


def test_source_page_escapes_hostile_text(client: TestClient, session: Session) -> None:
    source = make_source(session, approved_at=None)  # type: ignore[arg-type]
    source.last_error = "<script>alert(1)</script>"
    source.name = '"><img src=x onerror=alert(1)>'
    session.flush()
    signed_in(client, make_operator(session))
    page = client.get(f"/admin/sources/{source.id}").text
    assert "<script>alert(1)" not in page and "<img src=x" not in page


def test_unknown_source_is_404(client: TestClient, session: Session) -> None:
    signed_in(client, make_operator(session))
    assert client.get("/admin/sources/999999").status_code == 404


# --- approval, audit and the fetch gate -------------------------------------------------------


class FakeAdapter:
    slug_prefix = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def discover(self, source: Source, ctx: AdapterContext) -> list[DiscoveredItem]:
        self.calls += 1
        return [DiscoveredItem("https://x.example/a")]

    def process(self, doc, ctx) -> ProcessResult:  # type: ignore[no-untyped-def]
        return ProcessResult()


def run_fetch(session: Session, source: Source) -> None:
    job = queue.ClaimedJob(
        id=1, kind="fetch_source", payload={"source_id": source.id}, attempts=0, max_attempts=5
    )
    fetch_source_module.fetch_source(JobContext(session=session, job=job, worker_id="test"))


def test_source_cannot_be_fetched_until_approved_in_the_console(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FakeAdapter()
    monkeypatch.setattr(fetch_source_module, "get_adapter", lambda name: adapter)
    monkeypatch.setattr(fetch_source_module, "get_store", lambda: object())
    source = make_source(session, approved_at=None)  # type: ignore[arg-type]

    run_fetch(session, source)
    assert adapter.calls == 0  # refused: nothing approved yet

    account = make_operator(session)
    signed_in(client, account)
    url = f"/admin/sources/{source.id}/permissions/1/approve"
    response = client.post(url, data={"terms_reviewed": "on"}, headers=ORIGIN)
    assert response.status_code == 303

    run_fetch(session, source)
    assert adapter.calls == 1
    assert (
        session.scalar(select(func.count()).select_from(Job).where(Job.kind == "process_document"))
        == 1
    )


def test_approval_records_who_when_and_writes_an_audit_row(
    client: TestClient, session: Session
) -> None:
    source = make_source(session, approved_at=None)  # type: ignore[arg-type]
    account = make_operator(session)
    signed_in(client, account)
    client.post(
        f"/admin/sources/{source.id}/permissions/1/approve",
        data={"terms_reviewed": "on"},
        headers=ORIGIN,
    )
    permission = current_permission(session, source.id)
    assert permission is not None
    assert permission.approved_by_operator_id == account.operator.id
    assert permission.terms_checked_at is not None and permission.review_due_at is not None
    row = session.scalars(
        select(AuditLog).where(AuditLog.action == "source_permission.approve")
    ).one()
    assert row.operator_id == account.operator.id
    assert row.target_kind == "source_permission" and row.target_id == permission.id
    assert row.before["in_force"] is None  # type: ignore[index]
    assert row.after["in_force"]["version"] == 1  # type: ignore[index]
    assert row.after["source"] == source.slug  # type: ignore[index]


def test_approval_needs_the_terms_confirmation(client: TestClient, session: Session) -> None:
    source = make_source(session, approved_at=None)  # type: ignore[arg-type]
    signed_in(client, make_operator(session))
    response = client.post(
        f"/admin/sources/{source.id}/permissions/1/approve", data={}, headers=ORIGIN
    )
    assert response.status_code == 400 and "reviewed" in response.text
    assert current_permission(session, source.id) is None
    assert "source_permission.approve" not in audit_actions(session)


def test_an_approved_version_cannot_be_approved_again_and_a_stale_one_is_refused(
    client: TestClient, session: Session
) -> None:
    source = make_source(session, approved_at=None)  # type: ignore[arg-type]
    session.add(
        SourcePermission(
            source_id=source.id,
            version=2,
            may_collect=False,
            may_store_full_text=False,
            may_republish_numbers=False,
        )  # fmt: skip
    )
    session.flush()
    signed_in(client, make_operator(session))
    base_url = f"/admin/sources/{source.id}/permissions"
    stale = client.post(f"{base_url}/1/approve", data={"terms_reviewed": "on"}, headers=ORIGIN)
    assert stale.status_code == 400 and "newest" in stale.text
    ok = client.post(f"{base_url}/2/approve", data={"terms_reviewed": "on"}, headers=ORIGIN)
    assert ok.status_code == 303
    again = client.post(f"{base_url}/2/approve", data={"terms_reviewed": "on"}, headers=ORIGIN)
    assert again.status_code == 400 and "already approved" in again.text


def test_publishing_a_new_version_withdraws_permission_and_is_audited(
    client: TestClient, session: Session
) -> None:
    source = make_source(session, approved_at=datetime.now(UTC))  # type: ignore[arg-type]
    signed_in(client, make_operator(session))
    response = client.post(
        f"/admin/sources/{source.id}/permissions",
        data={"rights_basis": "Withdrawn after a takedown request", "link_required": "on"},
        headers=ORIGIN,
    )
    assert response.status_code == 303
    permission = current_permission(session, source.id)
    assert permission is not None and permission.version == 2 and not permission.may_collect
    row = session.scalars(
        select(AuditLog).where(AuditLog.action == "source_permission.new_version")
    ).one()
    assert row.before["in_force"]["may_collect"] is True  # type: ignore[index]
    assert row.after["in_force"]["may_collect"] is False  # type: ignore[index]
    # version 1 is untouched
    v1 = session.scalars(select(SourcePermission).where(SourcePermission.version == 1)).one()
    assert v1.may_collect is True


def test_new_version_validates_input_and_writes_nothing_on_error(
    client: TestClient, session: Session
) -> None:
    source = make_source(session, approved_at=None)  # type: ignore[arg-type]
    signed_in(client, make_operator(session))
    url = f"/admin/sources/{source.id}/permissions"
    good = {"may_collect": "on", "rights_basis": "Public statistics", "terms_reviewed": "on"}
    for bad in (
        {"terms_url": "javascript:alert(1)"},
        {"max_quote_chars": "-5"},
        {"retention_days": "soon"},
        {"rights_basis": ""},
        {"terms_reviewed": ""},
    ):
        data = {**good, **bad}
        if not data["terms_reviewed"]:
            del data["terms_reviewed"]
        response = client.post(url, data=data, headers=ORIGIN)
        assert response.status_code == 400, bad
    assert session.scalar(select(func.count()).select_from(SourcePermission)) == 1
    assert audit_actions(session) == ["operator.sign_in"]


def test_pause_and_resume_are_audited(client: TestClient, session: Session) -> None:
    source = make_source(session, approved_at=datetime.now(UTC))  # type: ignore[arg-type]
    signed_in(client, make_operator(session))
    assert client.post(f"/admin/sources/{source.id}/pause", headers=ORIGIN).status_code == 303
    assert source.active is False
    assert client.post(f"/admin/sources/{source.id}/pause", headers=ORIGIN).status_code == 400
    assert client.post(f"/admin/sources/{source.id}/resume", headers=ORIGIN).status_code == 303
    assert source.active is True
    assert audit_actions(session) == ["operator.sign_in", "source.pause", "source.resume"]


def test_editors_can_view_and_pause_but_not_approve_publish_or_resume(
    client: TestClient, session: Session
) -> None:
    source = make_source(session, approved_at=None)  # type: ignore[arg-type]
    signed_in(client, make_operator(session, "editor@example.org", "editor"))
    page = client.get(f"/admin/sources/{source.id}").text
    assert "Only an admin can approve" in page and "Approve version" not in page
    base_url = f"/admin/sources/{source.id}"
    assert (
        client.post(
            f"{base_url}/permissions/1/approve", data={"terms_reviewed": "on"}, headers=ORIGIN
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{base_url}/permissions", data={"may_collect": "on"}, headers=ORIGIN
        ).status_code
        == 403
    )
    assert client.post(f"{base_url}/pause", headers=ORIGIN).status_code == 303
    assert client.post(f"{base_url}/resume", headers=ORIGIN).status_code == 403
    assert current_permission(session, source.id) is None
    assert audit_actions(session) == ["operator.sign_in", "source.pause"]


def test_audit_page_lists_changes_newest_first(client: TestClient, session: Session) -> None:
    source = make_source(session, approved_at=datetime.now(UTC))  # type: ignore[arg-type]
    signed_in(client, make_operator(session))
    client.post(f"/admin/sources/{source.id}/pause", headers=ORIGIN)
    page = client.get("/admin/audit").text
    assert page.index("source.pause") < page.index("operator.sign_in")
    assert "ops@example.org" in page


def test_audit_row_and_change_roll_back_together(session: Session) -> None:
    from africasignal.sources import console

    source = make_source(session, approved_at=None)  # type: ignore[arg-type]
    operator = make_operator(session).operator
    before = session.scalar(select(func.count()).select_from(AuditLog))
    with pytest.raises(console.ConsoleError):
        console.approve_permission(session, operator, source.id, 1, terms_reviewed=False)
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before
    assert current_permission(session, source.id) is None


# --- CLI ----------------------------------------------------------------------------------------


def test_create_operator_cli_prints_a_working_totp_secret(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(admin_cli, "session_scope", fake_scope)
    assert (
        admin_cli.main(["create-operator", "--email", "New@Example.org", "--role", "editor"]) == 0
    )
    out = capsys.readouterr().out
    secret = re.search(r"secret: (\S+)", out).group(1)  # type: ignore[union-attr]
    operator = session.scalars(select(Operator).where(Operator.email == "new@example.org")).one()
    assert operator.role == "editor" and secret not in operator.totp_secret_enc
    assert operators.decrypt_totp_secret(operator.totp_secret_enc) == secret
    # duplicate and weak passwords are refused
    assert admin_cli.main(["create-operator", "--email", "new@example.org"]) == 1
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    assert admin_cli.main(["create-operator", "--email", "other@example.org"]) == 1
    # disable and enable
    assert admin_cli.main(["disable-operator", "--email", "new@example.org"]) == 0
    assert operator.disabled_at is not None
    assert admin_cli.main(["enable-operator", "--email", "new@example.org"]) == 0
    assert operator.disabled_at is None
    assert admin_cli.main(["disable-operator", "--email", "ghost@example.org"]) == 1
