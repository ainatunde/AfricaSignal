"""Operator accounts: argon2id passwords, TOTP second factor, sign-in throttling (spec B3.8, B11.5).

The TOTP secret is stored encrypted (Fernet) under a key derived from ``SECRET_KEY``, so a copy of
the database alone is not enough to generate codes. Sign-in always runs the password check and the
code check and gives one generic failure, so a caller learns nothing about which part was wrong or
whether the account exists.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass, field

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.config import get_settings
from africasignal.models import Operator

MIN_PASSWORD_LENGTH = 12
ROLES = ("admin", "editor")
TOTP_STEP_SECONDS = 30
ISSUER = "AfricaSignal"

# Used only when ENV=development and SECRET_KEY is unset, so the CLI and the web process (two
# processes) agree on the key. Outside development an empty SECRET_KEY stops startup.
_DEV_SECRET_KEY = "africasignal-development-only-secret-key"  # noqa: S105

_hasher = PasswordHasher()  # argon2id with the library's current default cost
_dummy_hash: str | None = None


def secret_key() -> str:
    """The application secret. Refuses to fall back to a known key outside development."""
    settings = get_settings()
    if settings.secret_key:
        return settings.secret_key
    if settings.env == "development":
        return _DEV_SECRET_KEY
    raise RuntimeError("SECRET_KEY is not set")


def derive_key(purpose: str) -> bytes:
    """A 32-byte key for one purpose, so the TOTP cipher and the cookie signer never share a key."""
    return hmac.new(secret_key().encode(), purpose.encode(), hashlib.sha256).digest()


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive_key("operator-totp-secret")))


def encrypt_totp_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_totp_secret(token: str) -> str | None:
    """The plain secret, or None when the token can't be decrypted (wrong key, corrupt value)."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _verify_password(stored_hash: str | None, password: str) -> bool:
    """Check ``password``. With no stored hash it checks against a throwaway one, so a missing
    account costs the same time as a wrong password."""
    global _dummy_hash
    if stored_hash is None:
        if _dummy_hash is None:
            _dummy_hash = _hasher.hash("africasignal-no-such-operator")
        stored_hash = _dummy_hash
        try:
            _hasher.verify(stored_hash, password)
        except (VerificationError, InvalidHashError):
            pass
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def normalise_email(email: str) -> str:
    return email.strip().lower()


def validate_password(password: str) -> str | None:
    """A reason the password is unacceptable, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password.strip() != password or not password.strip():
        return "The password must not start or end with a space."
    return None


def new_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(email: str, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def create_operator(session: Session, email: str, password: str, role: str) -> tuple[Operator, str]:
    """Create an operator and return it with the plain TOTP secret, shown once to the new
    operator for their authenticator app. Raises ``ValueError`` for bad input or a duplicate."""
    email = normalise_email(email)
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError("that is not an email address")
    if role not in ROLES:
        raise ValueError(f"role must be one of: {', '.join(ROLES)}")
    problem = validate_password(password)
    if problem:
        raise ValueError(problem)
    if session.scalars(select(Operator.id).where(Operator.email == email)).first() is not None:
        raise ValueError(f"an operator with the email {email} already exists")
    secret = new_totp_secret()
    operator = Operator(
        email=email,
        password_hash=hash_password(password),
        totp_secret_enc=encrypt_totp_secret(secret),
        role=role,
    )
    session.add(operator)
    session.flush()
    return operator, secret


# --- TOTP ---------------------------------------------------------------------------------------

# Last accepted time step per operator, so a code can't be replayed inside its validity window.
# Process-local, which is enough for one web process; a second process would need a shared store.
_last_step: dict[int, int] = {}


def verify_totp(operator_id: int, secret: str, code: str, now: float | None = None) -> bool:
    """Accept the current code or one step either side (clock drift), each code only once."""
    code = code.strip().replace(" ", "")
    if not (code.isascii() and code.isdigit() and len(code) == 6):
        return False
    now = time.time() if now is None else now
    totp = pyotp.TOTP(secret)
    step = int(now // TOTP_STEP_SECONDS)
    last = _last_step.get(operator_id, -1)
    matched = None
    for candidate in (step - 1, step, step + 1):
        # Compare against every candidate, without stopping at the first, to keep timing flat.
        if hmac.compare_digest(totp.at(candidate * TOTP_STEP_SECONDS), code):
            matched = candidate
    if matched is None or matched <= last:
        return False
    _last_step[operator_id] = matched
    return True


# --- throttle -----------------------------------------------------------------------------------


@dataclass
class LoginThrottle:
    """Refuse sign-in for an email after too many failures in a window. In memory, so it resets
    when the process restarts; the client IP is not used (it is not stored anywhere, and behind a
    proxy it would be the proxy's)."""

    max_failures: int = 5
    window_seconds: float = 900.0
    max_tracked: int = 10_000
    _failures: dict[str, list[float]] = field(default_factory=dict)

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window_seconds]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return len(self._recent(key, now)) >= self.max_failures

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if key not in self._failures and len(self._failures) >= self.max_tracked:
            # Drop entries whose window has passed; if the table is still full, forget the oldest.
            for old in [k for k in self._failures if not self._recent(k, now)]:
                self._failures.pop(old, None)
            if len(self._failures) >= self.max_tracked:
                self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(key, []).append(now)

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)


throttle = LoginThrottle()


# --- sign-in ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SignInResult:
    operator: Operator | None
    throttled: bool = False


def authenticate(
    session: Session,
    email: str,
    password: str,
    code: str,
    now: float | None = None,
) -> SignInResult:
    """Password and TOTP code together. On success returns the operator; otherwise ``operator`` is
    None and ``throttled`` says whether the refusal was because of too many recent failures."""
    key = normalise_email(email)
    if throttle.blocked(key, now):
        return SignInResult(None, throttled=True)

    operator = session.scalars(select(Operator).where(Operator.email == key)).first()
    password_ok = _verify_password(operator.password_hash if operator else None, password)
    secret = decrypt_totp_secret(operator.totp_secret_enc) if operator else None
    # verify_totp consumes the code, so only run it when the password was right: a wrong password
    # must not burn the real operator's current code.
    code_ok = bool(
        operator and secret and password_ok and verify_totp(operator.id, secret, code, now)
    )
    if not (operator and password_ok and code_ok and operator.disabled_at is None):
        throttle.record_failure(key, now)
        return SignInResult(None)
    throttle.clear(key)
    if _hasher.check_needs_rehash(operator.password_hash):
        operator.password_hash = hash_password(password)
    return SignInResult(operator)
