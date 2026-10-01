"""The signed-in reader: the ``as_session`` cookie, and the small helpers the account, follow and
feedback routes share.

The cookie holds a random session token; the database holds only its hash (``session`` table,
30 days). It is ``HttpOnly`` and ``SameSite=Lax``, so a cross-site form post does not carry it
(the origin guard is a second layer).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from africasignal.config import get_settings
from africasignal.models import AppUser
from africasignal.publish.login_tokens import SESSION_TTL, user_for_session
from africasignal.web.analytics import SESSION_COOKIE
from africasignal.web.session_dep import get_db

NEXT_COOKIE = "as_next"
NEXT_COOKIE_SECONDS = 15 * 60

Db = Annotated[Session, Depends(get_db)]


def now() -> datetime:
    return datetime.now(UTC)


def current_user(request: Request, db: Db) -> AppUser | None:
    token = request.cookies.get(SESSION_COOKIE)
    return user_for_session(db, token, now()) if token else None


CurrentUser = Annotated[AppUser | None, Depends(current_user)]


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
        httponly=True,
        samesite="lax",
        secure=get_settings().env != "development",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def safe_next(value: str | None) -> str | None:
    """A local path (``/s/abc``) or ``None``. Anything that could leave the site is refused."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):  # a tab or newline in "/\t/host"
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or len(value) > 300:
        return None
    return value


def sign_in_redirect(next_path: str | None = None) -> Response:
    """Send a signed-out reader to the sign-in page, remembering where they were going."""
    response = RedirectResponse("/signin", status_code=303)
    remember_next(response, next_path)
    return response


def remember_next(response: Response, next_path: str | None) -> None:
    target = safe_next(next_path)
    if target is None:
        return
    response.set_cookie(
        NEXT_COOKIE,
        target,
        max_age=NEXT_COOKIE_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=get_settings().env != "development",
    )


def private_headers(response: Response, *, sensitive: bool = False) -> Response:
    """Personal pages are never stored by a shared cache. ``sensitive`` pages (a sign-in link, an
    unsubscribe link) also stop the browser sending their address to another site as a referrer.
    ``same-origin``, not ``no-referrer``: with ``no-referrer`` browsers send ``Origin: null`` on the
    page's own form post and the origin guard refuses it."""
    response.headers["Cache-Control"] = "no-store" if sensitive else "private, no-cache"
    if sensitive:
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response
