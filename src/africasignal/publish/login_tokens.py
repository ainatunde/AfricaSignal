"""Magic-link sign-in: login tokens and sessions (plan B3.7, AS-031).

Only hashes are stored. The raw login token goes into the outbox payload (and is scrubbed once
the email is sent); the raw session token is returned to the web layer to set as a cookie.

Mail scanners often open links before the person does. A sign-in page that consumes the token on
GET would be used up by the scanner, so the web layer should show a confirm button and call
``consume_login_token`` on the POST.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal.models import AppUser, LoginToken, UserSession
from africasignal.publish import email_render
from africasignal.publish.email import EmailSendError
from africasignal.publish.outbox import enqueue_email

log = logging.getLogger("africasignal.login")

LOGIN_TOKEN_TTL = timedelta(minutes=15)
SESSION_TTL = timedelta(days=30)
MAX_LOGIN_REQUESTS_PER_HOUR = 5  # per email address; the web layer also limits per client

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 254


def normalise_email(raw: str) -> str | None:
    """The trimmed, lower-cased address, or ``None`` when it is not plausibly an email."""
    email = raw.strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(email):
        return None
    return email


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def request_login(session: Session, raw_email: str, now: datetime) -> bool:
    """Queue a sign-in email, creating the account on first use. The caller commits.

    Returns True when an email was queued. The web layer must answer "check your email" either
    way, so the response does not reveal whether an address is registered or rate limited.
    """
    email = normalise_email(raw_email)
    if email is None:
        return False
    session.execute(
        pg_insert(AppUser)
        .values(email=email)
        .on_conflict_do_nothing(index_elements=[AppUser.email])
    )
    user = session.scalars(select(AppUser).where(AppUser.email == email)).one()
    if user.deleted_at is not None:
        return False
    recent = session.scalar(
        select(func.count())
        .select_from(LoginToken)
        .where(
            LoginToken.user_id == user.id,
            # issued within the last hour; expires_at = issue time + TTL uses the caller's clock
            LoginToken.expires_at > now + LOGIN_TOKEN_TTL - timedelta(hours=1),
        )
    )
    if (recent or 0) >= MAX_LOGIN_REQUESTS_PER_HOUR:
        return False

    try:
        base = email_render.resolve_base_url(session)  # read now: the console may have changed it
    except EmailSendError:
        log.error("sign-in email not queued: the public address is not set in the console")
        return False

    raw = secrets.token_urlsafe(32)
    token = LoginToken(
        user_id=user.id, token_sha256=hash_token(raw), expires_at=now + LOGIN_TOKEN_TTL
    )
    session.add(token)
    session.flush()
    enqueue_email(
        session,
        "email_login",
        {"user_id": user.id, "link": email_render.login_url(raw, base)},
        dedupe_key=f"login:{token.id}",
    )
    return True


@dataclass(frozen=True)
class SignedIn:
    user_id: int
    session_token: str  # raw; set it as the session cookie
    expires_at: datetime


def consume_login_token(session: Session, raw: str, now: datetime) -> SignedIn | None:
    """Use a login token once. Marks the email verified and opens a 30-day session.
    Returns ``None`` for an unknown, expired or already used token. The caller commits."""
    user_id = session.execute(
        update(LoginToken)
        .where(
            LoginToken.token_sha256 == hash_token(raw),
            LoginToken.used_at.is_(None),
            LoginToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(LoginToken.user_id)
    ).scalar_one_or_none()
    if user_id is None:
        return None
    user = session.get(AppUser, user_id)
    if user is None or user.deleted_at is not None:
        return None
    if user.email_verified_at is None:
        user.email_verified_at = now
    return _open_session(session, user.id, now)


def _open_session(session: Session, user_id: int, now: datetime) -> SignedIn:
    raw = secrets.token_urlsafe(32)
    expires = now + SESSION_TTL
    session.add(UserSession(user_id=user_id, token_sha256=hash_token(raw), expires_at=expires))
    session.flush()
    return SignedIn(user_id, raw, expires)


def user_for_session(session: Session, raw: str, now: datetime) -> AppUser | None:
    """The signed-in user for a session cookie, or ``None``."""
    return session.scalars(
        select(AppUser)
        .join(UserSession, UserSession.user_id == AppUser.id)
        .where(
            UserSession.token_sha256 == hash_token(raw),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
            AppUser.deleted_at.is_(None),
        )
    ).one_or_none()


def revoke_session(session: Session, raw: str, now: datetime) -> None:
    session.execute(
        update(UserSession)
        .where(UserSession.token_sha256 == hash_token(raw), UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
