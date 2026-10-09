"""Shared web dependencies: the database session and the operator session cookie.

The console session is a signed, stateless cookie: ``base64url(json).base64url(hmac-sha256)``. It
holds the operator id, when the session began, when it was last used, a short fingerprint of the
operator's password hash and the operator's session epoch. Every request re-reads the operator, so
disabling an operator, changing their password, or signing out (which raises the epoch) ends all
of their sessions at once, including a copy of the cookie that was stolen; an idle session lapses
after ``IDLE_SECONDS`` and any session after ``ABSOLUTE_SECONDS``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from africasignal.config import get_settings
from africasignal.db import session_scope
from africasignal.models import Operator
from africasignal.operators import derive_key

COOKIE_NAME = "as_admin"
IDLE_SECONDS = 3600
ABSOLUTE_SECONDS = 12 * 3600
LOGIN_PATH = "/admin/login"


def get_db() -> Iterator[Session]:
    """One session per request, committed when the request succeeds."""
    with session_scope() as session:
        yield session


# --- cookie -------------------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if _b64(raw) != text:
        raise ValueError("non-canonical base64")
    return raw


def _sign(body: str) -> str:
    return _b64(hmac.new(derive_key("admin-session"), body.encode(), hashlib.sha256).digest())


def fingerprint(operator: Operator) -> str:
    return hashlib.sha256(operator.password_hash.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class SessionData:
    operator_id: int
    started: int
    seen: int
    fp: str
    epoch: int


def encode_session(data: SessionData) -> str:
    body = _b64(
        json.dumps(
            {
                "op": data.operator_id,
                "iat": data.started,
                "seen": data.seen,
                "fp": data.fp,
                "ep": data.epoch,
            },
            separators=(",", ":"),
        ).encode()
    )
    return f"{body}.{_sign(body)}"


def decode_session(value: str | None, now: float | None = None) -> SessionData | None:
    """The session in a cookie, or None when it is missing, forged, malformed or expired."""
    if not value or value.count(".") != 1:
        return None
    body, signature = value.split(".")
    if not hmac.compare_digest(_sign(body), signature):
        return None
    try:
        raw = json.loads(_unb64(body))
        data = SessionData(
            int(raw["op"]), int(raw["iat"]), int(raw["seen"]), str(raw["fp"]), int(raw["ep"])
        )
    except (ValueError, KeyError, TypeError):
        return None
    now = time.time() if now is None else now
    if now - data.seen > IDLE_SECONDS or now - data.started > ABSOLUTE_SECONDS:
        return None
    return data


def new_session_cookie(operator: Operator, now: float | None = None) -> str:
    now = int(time.time() if now is None else now)
    return encode_session(
        SessionData(operator.id, now, now, fingerprint(operator), operator.session_epoch or 0)
    )


def refreshed_cookie(data: SessionData, now: float | None = None) -> str:
    """The same session with ``seen`` moved to now, so activity keeps it alive."""
    now = int(time.time() if now is None else now)
    return encode_session(SessionData(data.operator_id, data.started, now, data.fp, data.epoch))


def cookie_secure() -> bool:
    return get_settings().env != "development"


# --- current operator ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Authenticated:
    operator: Operator
    session: SessionData


def _to_login() -> HTTPException:
    return HTTPException(
        status_code=303, detail="Sign in required", headers={"Location": LOGIN_PATH}
    )


DbSession = Annotated[Session, Depends(get_db)]


def current_operator(request: Request, db: DbSession) -> Authenticated:
    data = decode_session(request.cookies.get(COOKIE_NAME))
    if data is None:
        raise _to_login()
    operator = db.get(Operator, data.operator_id)
    if (
        operator is None
        or operator.disabled_at is not None
        or not hmac.compare_digest(fingerprint(operator), data.fp)
        or (operator.session_epoch or 0) != data.epoch
    ):
        raise _to_login()
    return Authenticated(operator, data)


CurrentOperator = Annotated[Authenticated, Depends(current_operator)]


def require_admin(auth: CurrentOperator) -> Authenticated:
    if auth.operator.role != "admin":
        raise HTTPException(status_code=403, detail="This needs an admin operator.")
    return auth


AdminOperator = Annotated[Authenticated, Depends(require_admin)]
