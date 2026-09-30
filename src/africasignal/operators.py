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
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.config import get_settings
from africasignal.models import Operator, OperatorSignInFailure

log = logging.getLogger("africasignal.operators")

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


def matching_totp_step(secret: str, code: str, now: float | None = None) -> int | None:
    """The time step whose code equals ``code`` (the current step or one either side, for clock
    drift), or None. Pure: whether the step was already used is ``authenticate``'s business."""
    code = code.strip().replace(" ", "")
    if not (code.isascii() and code.isdigit() and len(code) == 6):
        return None
    now = time.time() if now is None else now
    totp = pyotp.TOTP(secret)
    step = int(now // TOTP_STEP_SECONDS)
    matched = None
    for candidate in (step - 1, step, step + 1):
        # Compare against every candidate, without stopping at the first, to keep timing flat.
        if hmac.compare_digest(totp.at(candidate * TOTP_STEP_SECONDS), code):
            matched = candidate
    return matched


def _use_totp_step(session: Session, operator: Operator, step: int) -> bool:
    """Record ``step`` as the newest accepted for the operator. False when it is not newer than the
    last one, so a code is good once, in every process (one conditional UPDATE, no race)."""
    result = session.execute(
        update(Operator)
        .where(
            Operator.id == operator.id,
            or_(Operator.last_totp_step.is_(None), Operator.last_totp_step < step),
        )
        .values(last_totp_step=step)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


# --- throttle -----------------------------------------------------------------------------------
#
# Two counters, both stored in the database so a restart or a second process does not reset them:
#
# * per account and client: 5 failures in 15 minutes stop that client (a keyed hash of its
#   address, never the address) trying that account. This is the brute-force limit.
# * per account, all clients: 50 failures in 15 minutes stop everyone, for the same 15 minutes.
#   Password and code are both needed, so 50 guesses gain nothing, and the limit is high enough
#   that a stranger must keep up real traffic to hold an operator out. It never gets longer by
#   trying more: refused attempts are not counted, and each failure ages out after 15 minutes.
#
# A refused attempt costs nothing and is not recorded, so a flood cannot grow the table; failures
# are only recorded up to ``MAX_TRACKED_FAILURES`` rows. ``python -m africasignal.admin
# unlock-operator --email ...`` clears an account's failures at once.

CLIENT_MAX_FAILURES = 5
ACCOUNT_MAX_FAILURES = 50
FAILURE_WINDOW = timedelta(minutes=15)
MAX_TRACKED_FAILURES = 50_000


def client_key(address: str) -> str:
    """A keyed hash of a client address: stable for the throttle, meaningless outside it."""
    return hmac.new(derive_key("operator-throttle"), address.encode(), hashlib.sha256).hexdigest()[
        :32
    ]


@dataclass(frozen=True)
class _Counts:
    client: int
    account: int

    @property
    def blocked(self) -> bool:
        return self.client >= CLIENT_MAX_FAILURES or self.account >= ACCOUNT_MAX_FAILURES


def _failure_counts(session: Session, email: str, client: str, since: datetime) -> _Counts:
    in_window = (OperatorSignInFailure.email == email) & (OperatorSignInFailure.at > since)
    account = session.scalar(select(func.count()).where(in_window)) or 0
    from_client = (
        session.scalar(
            select(func.count()).where(in_window, OperatorSignInFailure.client_key == client)
        )
        or 0
    )
    return _Counts(client=from_client, account=account)


def unlock(session: Session, email: str) -> int:
    """Forget an account's recorded failures. Returns how many there were."""
    result = session.execute(
        delete(OperatorSignInFailure).where(OperatorSignInFailure.email == normalise_email(email))
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


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
    client: str = "",
    now: float | None = None,
) -> SignInResult:
    """Password and TOTP code together. On success returns the operator; otherwise ``operator`` is
    None and ``throttled`` says whether the refusal was because of too many recent failures.
    ``client`` is ``client_key(address)`` of whoever is signing in."""
    key = normalise_email(email)
    moment = datetime.fromtimestamp(time.time() if now is None else now, UTC)
    since = moment - FAILURE_WINDOW
    session.execute(delete(OperatorSignInFailure).where(OperatorSignInFailure.at <= since))
    if _failure_counts(session, key, client, since).blocked:
        return SignInResult(None, throttled=True)

    operator = session.scalars(select(Operator).where(Operator.email == key)).first()
    password_ok = _verify_password(operator.password_hash if operator else None, password)
    secret = decrypt_totp_secret(operator.totp_secret_enc) if operator else None
    # The code is only checked, and used up, when the password was right: a wrong password must
    # not burn the real operator's current code.
    step = matching_totp_step(secret, code, now) if operator and secret and password_ok else None
    code_ok = step is not None and operator is not None and _use_totp_step(session, operator, step)
    if not (operator and password_ok and code_ok and operator.disabled_at is None):
        _record_failure(session, operator, key, client, moment, since)
        return SignInResult(None)
    session.execute(
        delete(OperatorSignInFailure).where(
            OperatorSignInFailure.email == key, OperatorSignInFailure.client_key == client
        )
    )
    if _hasher.check_needs_rehash(operator.password_hash):
        operator.password_hash = hash_password(password)
    return SignInResult(operator)


def _record_failure(
    session: Session,
    operator: Operator | None,
    email: str,
    client: str,
    moment: datetime,
    since: datetime,
) -> None:
    if (session.scalar(select(func.count()).select_from(OperatorSignInFailure)) or 0) < (
        MAX_TRACKED_FAILURES
    ):
        session.add(OperatorSignInFailure(email=email, client_key=client, at=moment))
        session.flush()
    if operator is None:
        return  # an address that is not an operator has no audit trail to write to
    audit.record(session, operator, "operator.sign_in_failed", "operator", operator.id)
    counts = _failure_counts(session, email, client, since)
    if counts.client == CLIENT_MAX_FAILURES or counts.account == ACCOUNT_MAX_FAILURES:
        audit.record(session, operator, "operator.sign_in_locked", "operator", operator.id)
        log.warning("console sign-in locked for an operator after repeated failures")


def revoke_sessions(session: Session, operator: Operator) -> None:
    """End every console session of the operator: cookies of the old epoch stop working."""
    operator.session_epoch = (operator.session_epoch or 0) + 1
    session.flush()
