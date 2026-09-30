"""Operator primitives that need no database: passwords, TOTP, encryption, throttle, cookie."""

from __future__ import annotations

import pyotp
import pytest

from africasignal import operators
from africasignal.models import Operator
from africasignal.operators import LoginThrottle
from africasignal.web import deps


@pytest.fixture(autouse=True)
def _clean() -> None:
    operators._last_step.clear()
    operators.throttle._failures.clear()


def test_password_policy() -> None:
    assert operators.validate_password("short") is not None
    assert operators.validate_password(" leading-space-password") is not None
    assert operators.validate_password("a-long-enough-password") is None


def test_password_hash_is_argon2id_and_verifies() -> None:
    hashed = operators.hash_password("correct horse battery")
    assert hashed.startswith("$argon2id$")
    assert operators._verify_password(hashed, "correct horse battery")
    assert not operators._verify_password(hashed, "wrong horse battery")
    assert not operators._verify_password(None, "anything at all")
    assert not operators._verify_password("not a hash", "anything at all")


def test_totp_secret_is_encrypted_at_rest_and_round_trips() -> None:
    secret = operators.new_totp_secret()
    token = operators.encrypt_totp_secret(secret)
    assert secret not in token
    assert operators.decrypt_totp_secret(token) == secret
    assert operators.decrypt_totp_secret("garbage") is None


def test_totp_accepts_current_and_adjacent_steps_only() -> None:
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    now = 1_800_000_000.0
    assert operators.verify_totp(1, secret, totp.at(now), now)
    operators._last_step.clear()
    assert operators.verify_totp(1, secret, totp.at(now - 30), now)  # one step behind
    operators._last_step.clear()
    assert operators.verify_totp(1, secret, totp.at(now + 30), now)  # one step ahead
    operators._last_step.clear()
    assert not operators.verify_totp(1, secret, totp.at(now - 90), now)  # too old


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "١٢٣٤٥٦", "12 34 5"])
def test_totp_rejects_malformed_codes(code: str) -> None:
    assert not operators.verify_totp(1, pyotp.random_base32(), code, 1_800_000_000.0)


def test_totp_code_cannot_be_replayed() -> None:
    secret = pyotp.random_base32()
    now = 1_800_000_000.0
    code = pyotp.TOTP(secret).at(now)
    assert operators.verify_totp(7, secret, code, now)
    assert not operators.verify_totp(7, secret, code, now + 5)
    # a different operator is unaffected
    assert operators.verify_totp(8, secret, code, now)


def test_throttle_blocks_after_max_failures_and_recovers() -> None:
    t = LoginThrottle(max_failures=3, window_seconds=100)
    for i in range(3):
        assert not t.blocked("a@example.org", now=i)
        t.record_failure("a@example.org", now=i)
    assert t.blocked("a@example.org", now=10)
    assert not t.blocked("b@example.org", now=10)
    assert not t.blocked("a@example.org", now=200)  # window passed


def test_throttle_success_clears_and_table_is_bounded() -> None:
    t = LoginThrottle(max_failures=2, window_seconds=100, max_tracked=3)
    t.record_failure("a", now=0)
    t.clear("a")
    assert not t._failures
    for key in "abcdef":
        t.record_failure(key, now=1)
    assert len(t._failures) <= 3


def _operator(role: str = "admin") -> Operator:
    op = Operator(email="o@example.org", password_hash="hash-one", totp_secret_enc="x", role=role)
    op.id = 5
    return op


def test_session_cookie_round_trip_and_expiry() -> None:
    op = _operator()
    cookie = deps.new_session_cookie(op, now=1000)
    data = deps.decode_session(cookie, now=1100)
    assert data is not None and data.operator_id == 5 and data.fp == deps.fingerprint(op)
    assert deps.decode_session(cookie, now=1000 + deps.IDLE_SECONDS + 1) is None  # idle
    refreshed = deps.refreshed_cookie(data, now=1000 + deps.IDLE_SECONDS)
    assert deps.decode_session(refreshed, now=1000 + deps.IDLE_SECONDS + 100) is not None
    assert deps.decode_session(refreshed, now=1000 + deps.ABSOLUTE_SECONDS + 1) is None  # absolute


@pytest.mark.parametrize("bad", [None, "", "x", "a.b", "a.b.c"])
def test_session_cookie_rejects_junk(bad: str | None) -> None:
    assert deps.decode_session(bad) is None


def test_session_cookie_rejects_tampering() -> None:
    cookie = deps.new_session_cookie(_operator())
    body, sig = cookie.split(".")
    other_operator = _operator()
    other_operator.id = 1  # swap in another operator's id, keeping the old signature
    other = deps.new_session_cookie(other_operator).split(".")[0]
    assert deps.decode_session(f"{other}.{sig}") is None
    assert deps.decode_session(f"{body}.{sig[:-2]}AA") is None


def test_password_change_changes_the_fingerprint() -> None:
    a, b = _operator(), _operator()
    b.password_hash = "hash-two"
    assert deps.fingerprint(a) != deps.fingerprint(b)
