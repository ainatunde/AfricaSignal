"""Operator primitives that need no database: passwords, TOTP, encryption, cookie."""

from __future__ import annotations

import pyotp
import pytest

from africasignal import operators
from africasignal.models import Operator
from africasignal.web import deps


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
    step = int(now // operators.TOTP_STEP_SECONDS)
    assert operators.matching_totp_step(secret, totp.at(now), now) == step
    assert operators.matching_totp_step(secret, totp.at(now - 30), now) == step - 1
    assert operators.matching_totp_step(secret, totp.at(now + 30), now) == step + 1
    assert operators.matching_totp_step(secret, totp.at(now - 90), now) is None  # too old


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "١٢٣٤٥٦", "12 34 5"])
def test_totp_rejects_malformed_codes(code: str) -> None:
    assert operators.matching_totp_step(pyotp.random_base32(), code, 1_800_000_000.0) is None


def _operator(role: str = "admin") -> Operator:
    op = Operator(email="o@example.org", password_hash="hash-one", totp_secret_enc="x", role=role)
    op.id = 5
    op.session_epoch = 0
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


def test_a_raised_session_epoch_is_part_of_the_cookie() -> None:
    op = _operator()
    cookie = deps.new_session_cookie(op, now=1000)
    op.session_epoch = 1
    data = deps.decode_session(cookie, now=1100)
    assert data is not None and data.epoch == 0  # the cookie keeps the epoch it was issued under
    assert deps.decode_session(deps.new_session_cookie(op, now=1000), now=1100).epoch == 1  # type: ignore[union-attr]


def test_a_cookie_from_before_epochs_is_not_accepted() -> None:
    import base64
    import json

    body = (
        base64.urlsafe_b64encode(
            json.dumps({"op": 5, "iat": 1000, "seen": 1000, "fp": "x"}).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    assert deps.decode_session(f"{body}.{deps._sign(body)}", now=1100) is None
