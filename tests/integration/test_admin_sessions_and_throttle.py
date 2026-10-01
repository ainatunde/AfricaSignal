"""Console session revocation, the database-backed sign-in throttle and the client address behind
proxies (AS-042, findings S-03, S-04 and S-05)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal import admin as admin_cli
from africasignal import operators, settings_store
from africasignal.models import AuditLog, Operator, OperatorSignInFailure
from africasignal.web import client_address
from africasignal.web.app import create_app
from africasignal.web.deps import COOKIE_NAME, get_db

PASSWORD = "correct horse battery staple"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def _proxy_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_address, "hops_source", lambda: 1)  # one proxy in front
    client_address.reset_cache()


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        yield c


def make_operator(session: Session, email: str = "ops@example.org") -> tuple[Operator, str]:
    return operators.create_operator(session, email, PASSWORD, "admin")


def sign_in(
    client: TestClient,
    operator: Operator,
    secret: str,
    *,
    address: str = "198.51.100.1",
    **over: str,
) -> int:
    form = {"email": operator.email, "password": PASSWORD, "code": pyotp.TOTP(secret).now()}
    response = client.post(
        "/admin/login",
        data={**form, **over},
        headers={**ORIGIN, "X-Forwarded-For": address},
    )
    return response.status_code


def actions(session: Session) -> list[str]:
    return list(session.scalars(select(AuditLog.action).order_by(AuditLog.id)))


def failures(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(OperatorSignInFailure)) or 0


# --- sessions -----------------------------------------------------------------------------------


def test_signing_out_ends_every_copy_of_the_cookie(client: TestClient, session: Session) -> None:
    operator, secret = make_operator(session)
    assert sign_in(client, operator, secret) == 303
    stolen = client.cookies[COOKIE_NAME]
    assert client.get("/admin/sources").status_code == 200
    assert client.post("/admin/logout", headers=ORIGIN).status_code == 303
    assert client.get("/admin/sources").status_code == 303  # this browser is out
    client.cookies.set(COOKIE_NAME, stolen, path="/admin")  # the copy someone else kept
    assert client.get("/admin/sources").status_code == 303
    assert "operator.sign_out" in actions(session)


def test_a_new_sign_in_after_sign_out_works(client: TestClient, session: Session) -> None:
    operator, secret = make_operator(session)
    sign_in(client, operator, secret)
    client.post("/admin/logout", headers=ORIGIN)
    second = operators.authenticate(  # the next TOTP step, as a person would wait for it
        session,
        operator.email,
        PASSWORD,
        pyotp.TOTP(secret).at(datetime.now(UTC) + timedelta(seconds=30)),
        client="c",
        now=datetime.now(UTC).timestamp() + 30,
    )
    assert second.operator is not None


def test_revoking_sessions_ends_a_signed_in_operator(client: TestClient, session: Session) -> None:
    operator, secret = make_operator(session)
    sign_in(client, operator, secret)
    assert client.get("/admin/sources").status_code == 200
    operators.revoke_sessions(session, operator)
    assert client.get("/admin/sources").status_code == 303


def test_signing_out_without_a_session_changes_nothing(
    client: TestClient, session: Session
) -> None:
    operator, _ = make_operator(session)
    assert client.post("/admin/logout", headers=ORIGIN).status_code == 303
    assert operator.session_epoch == 0 and actions(session) == []


def test_a_code_cannot_be_used_twice_even_from_another_process(
    client: TestClient, session: Session
) -> None:
    """The last used step lives on the operator row, not in this process."""
    operator, secret = make_operator(session)
    code = pyotp.TOTP(secret).now()
    assert sign_in(client, operator, secret, code=code) == 303
    assert operator.last_totp_step is not None
    client.cookies.clear()
    assert sign_in(client, operator, secret, code=code) == 401


# --- throttle -----------------------------------------------------------------------------------


@pytest.fixture
def small_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(operators, "CLIENT_MAX_FAILURES", 2)
    monkeypatch.setattr(operators, "ACCOUNT_MAX_FAILURES", 5)


def test_one_client_is_stopped_but_another_still_signs_in(
    client: TestClient, session: Session, small_limits: None
) -> None:
    operator, secret = make_operator(session)
    for _ in range(2):
        assert (
            sign_in(client, operator, secret, address="203.0.113.7", password="wrong wrong wrong")
            == 401
        )
    assert (
        sign_in(client, operator, secret, address="203.0.113.7") == 429
    )  # even with good credentials
    assert sign_in(client, operator, secret, address="198.51.100.9") == 303  # the real operator


def test_a_stranger_cannot_hold_the_real_operator_out_for_long(
    client: TestClient, session: Session, small_limits: None
) -> None:
    operator, secret = make_operator(session)
    for n in range(5):  # five strangers, each below the per-client limit
        sign_in(client, operator, secret, address=f"203.0.113.{n}", password="wrong wrong wrong")
    assert sign_in(client, operator, secret, address="198.51.100.9") == 429
    # Refused attempts are not counted, so trying more does not make the lock longer.
    before = failures(session)
    for _ in range(10):
        sign_in(client, operator, secret, address="203.0.113.99", password="wrong wrong wrong")
    assert failures(session) == before
    # After the window passes, the real operator is in again.
    later = datetime.now(UTC).timestamp() + operators.FAILURE_WINDOW.total_seconds() + 60
    result = operators.authenticate(
        session,
        operator.email,
        PASSWORD,
        pyotp.TOTP(secret).at(later),
        client=operators.client_key("198.51.100.9"),
        now=later,
    )
    assert result.operator is not None and not result.throttled


def test_unlock_clears_the_failures_at_once(
    client: TestClient, session: Session, small_limits: None
) -> None:
    operator, secret = make_operator(session)
    for n in range(5):
        sign_in(client, operator, secret, address=f"203.0.113.{n}", password="wrong wrong wrong")
    assert sign_in(client, operator, secret, address="198.51.100.9") == 429
    assert operators.unlock(session, operator.email.upper()) == 5
    assert sign_in(client, operator, secret, address="198.51.100.9") == 303


def test_a_good_sign_in_clears_only_that_clients_failures(
    client: TestClient, session: Session, small_limits: None
) -> None:
    operator, secret = make_operator(session)
    sign_in(client, operator, secret, address="203.0.113.1", password="wrong wrong wrong")
    sign_in(client, operator, secret, address="198.51.100.9", password="wrong wrong wrong")
    assert sign_in(client, operator, secret, address="198.51.100.9") == 303
    clients = set(session.scalars(select(OperatorSignInFailure.client_key)))
    assert clients == {operators.client_key("203.0.113.1")}  # the stranger's failure stays


def test_failures_are_stored_with_a_hash_not_an_address(
    client: TestClient, session: Session
) -> None:
    operator, secret = make_operator(session)
    sign_in(client, operator, secret, address="203.0.113.77", password="wrong wrong wrong")
    row = session.scalars(select(OperatorSignInFailure)).one()
    assert "203.0.113" not in row.client_key and row.email == operator.email
    assert row.client_key == operators.client_key("203.0.113.77")


def test_failures_and_lockouts_are_audited_for_real_operators_only(
    client: TestClient, session: Session, small_limits: None
) -> None:
    operator, secret = make_operator(session)
    sign_in(client, operator, secret, address="203.0.113.1", password="wrong wrong wrong")
    sign_in(client, operator, secret, address="203.0.113.1", password="wrong wrong wrong")
    assert actions(session) == [
        "operator.sign_in_failed",
        "operator.sign_in_failed",
        "operator.sign_in_locked",
    ]
    before = len(actions(session))
    assert sign_in(client, operator, secret, address="203.0.113.1") == 429
    assert len(actions(session)) == before  # a refused attempt writes nothing
    ghost = Operator(
        email="ghost@example.org", password_hash="x", totp_secret_enc="x", role="admin"
    )
    assert sign_in(client, ghost, "JBSWY3DPEJBSWY3D", address="203.0.113.2") == 401
    assert len(actions(session)) == before and failures(session) == 3  # recorded, not audited


def test_an_unknown_address_is_throttled_like_a_known_one(
    client: TestClient, session: Session, small_limits: None
) -> None:
    ghost = Operator(
        email="ghost@example.org", password_hash="x", totp_secret_enc="x", role="admin"
    )
    codes = [sign_in(client, ghost, "JBSWY3DPEJBSWY3D", address="203.0.113.2") for _ in range(3)]
    assert codes == [401, 401, 429]


def test_the_table_stops_growing_at_its_cap(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(operators, "MAX_TRACKED_FAILURES", 3)
    for n in range(6):
        operators.authenticate(session, f"x{n}@example.org", "whatever", "000000", client="c")
    assert failures(session) == 3


def test_old_failures_are_deleted(session: Session) -> None:
    operators.authenticate(session, "a@example.org", "x", "000000", client="c", now=1_000.0)
    assert failures(session) == 1
    operators.authenticate(session, "b@example.org", "x", "000000", client="c", now=1_000.0 + 901)
    assert set(session.scalars(select(OperatorSignInFailure.email))) == {"b@example.org"}


def test_the_cli_revokes_sessions_and_unlocks(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(admin_cli, "session_scope", fake_scope)
    operator, secret = make_operator(session)
    sign_in(client, operator, secret)
    assert admin_cli.main(["revoke-sessions", "--email", "OPS@example.org"]) == 0
    assert client.get("/admin/sources").status_code == 303
    assert admin_cli.main(["revoke-sessions", "--email", "ghost@example.org"]) == 1
    sign_in(client, operator, secret, address="203.0.113.5", password="wrong wrong wrong")
    assert admin_cli.main(["unlock-operator", "--email", "ops@example.org"]) == 0
    assert failures(session) == 0


# --- client address behind proxies --------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "hops", "expected"),
    [
        (["203.0.113.5"], 1, "203.0.113.5"),
        (["1.1.1.1, 203.0.113.5"], 1, "203.0.113.5"),  # what the reader wrote is never used
        (["1.1.1.1", "198.51.100.2, 203.0.113.5"], 2, "198.51.100.2"),  # a CDN and a proxy
        (["203.0.113.5"], 2, None),  # fewer entries than proxies: the header is not trustworthy
        ([], 1, None),
        (["not-an-address"], 1, None),
        (["2001:db8::1"], 1, "2001:db8::1"),
        (["203.0.113.5"], 0, None),  # 0 never reads the header
    ],
)
def test_forwarded_client_takes_the_nth_entry_from_the_right(
    headers: list[str], hops: int, expected: str | None
) -> None:
    assert client_address.forwarded_client(headers, hops) == expected


def test_without_proxies_the_header_is_ignored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_address, "hops_source", lambda: 0)
    client_address.reset_cache()
    from africasignal.web.routes import api_v1

    monkeypatch.setattr(api_v1, "rate_limiter", api_v1.RateLimiter(limit=2, window_seconds=60))
    for n in range(3):  # a forged header per request must not buy a new allowance
        codes = client.get(
            "/v1/coverage", headers={"X-Forwarded-For": f"203.0.113.{n}"}
        ).status_code
    assert codes == 429


def test_with_a_proxy_each_reader_has_their_own_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from africasignal.web.routes import api_v1

    monkeypatch.setattr(api_v1, "rate_limiter", api_v1.RateLimiter(limit=1, window_seconds=60))
    first = client.get("/v1/coverage", headers={"X-Forwarded-For": "203.0.113.1"})
    again = client.get("/v1/coverage", headers={"X-Forwarded-For": "203.0.113.1"})
    other = client.get("/v1/coverage", headers={"X-Forwarded-For": "203.0.113.2"})
    forged = client.get("/v1/coverage", headers={"X-Forwarded-For": "198.51.100.66, 203.0.113.1"})
    assert (first.status_code, again.status_code) == (200, 429)
    assert other.status_code == 200
    assert forged.status_code == 429  # extra entries on the left change nothing


def test_the_hops_value_is_a_console_setting(session: Session) -> None:
    defn = settings_store.definition("trusted_proxy_hops")
    assert defn.group == "site" and defn.default == "0"
    assert settings_store.get_int(session, "trusted_proxy_hops") == 0
    with pytest.raises(settings_store.SettingError):
        settings_store.normalise(defn, "6")
    with pytest.raises(settings_store.SettingError):
        settings_store.normalise(defn, "-1")
    assert settings_store.normalise(defn, "2") == "2"


def test_the_real_reader_reads_the_setting_once_in_a_while(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(client_address, "hops_source", lambda: calls.append(1) or 1)
    client_address.reset_cache()
    assert client_address.trusted_proxy_hops() == 1 and client_address.trusted_proxy_hops() == 1
    assert len(calls) == 1
