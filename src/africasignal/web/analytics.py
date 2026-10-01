"""Page-view events and the anonymous visitor cookie (plan AS-034).

For each successful HTML page a person opens, this records a ``page_view`` (and a
``situation_view`` for a situation page) against the visitor's ``anon_id`` cookie, and gives a new
visitor their cookie. Nothing else about the request is kept: no address, no user agent, no URL.
Only the page kind, the situation, and ``?ref=`` when it is one of ``wa``, ``x``, ``email`` or
``share``. Crawlers and link previews (a WhatsApp preview fetches the page when a link is shared)
are not counted. A failure to record never affects the page.

Pages that carry a secret in the URL (``/signin/verify``, ``/unsubscribe``) are never recorded.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from africasignal import metrics
from africasignal.config import get_settings
from africasignal.models import Situation
from africasignal.publish.login_tokens import user_for_session

log = logging.getLogger("africasignal.web.analytics")

SESSION_COOKIE = "as_session"  # the signed-in user's session token (see routes/account.py)

# Path -> page kind, for the pages worth counting.
_PAGES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/"), "home"),
    (re.compile(r"/explore"), "explore"),
    (re.compile(r"/s/([^/]+)"), "situation"),
    (re.compile(r"/s/([^/]+)/history"), "history"),
    (re.compile(r"/places/[^/]+"), "place"),
    (re.compile(r"/coverage"), "coverage"),
    (re.compile(r"/about/method"), "method"),
    (re.compile(r"/following"), "following"),
    (re.compile(r"/account"), "account"),
)


def page_kind(path: str) -> tuple[str, str | None] | None:
    """(kind, situation slug) for a countable page path, else ``None``."""
    for pattern, kind in _PAGES:
        match = pattern.fullmatch(path)
        if match:
            return kind, (match.group(1) if kind in ("situation", "history") else None)
    return None


def set_anon_cookie(response: Response, anon_id: str) -> None:
    response.set_cookie(
        metrics.ANON_COOKIE,
        anon_id,
        max_age=metrics.ANON_COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=get_settings().env != "development",
    )
    # A page that sets a cookie must not be stored by a shared cache and served to someone else.
    response.headers["Cache-Control"] = "private, no-cache"


SessionFactory = Callable[[], AbstractContextManager[Session]]


def record_page_view(
    session: Session,
    *,
    kind: str,
    slug: str | None,
    anon_id: str,
    ref: str | None,
    session_token: str | None,
    now: datetime,
) -> None:
    user = user_for_session(session, session_token, now) if session_token else None
    situation_id = (
        session.scalar(select(Situation.id).where(Situation.slug == slug)) if slug else None
    )
    user_id = user.id if user else None
    metrics.record_event(
        session,
        "page_view",
        anon_id=anon_id,
        user_id=user_id,
        situation_id=situation_id,
        ref=ref,
        props={"page": kind},
        now=now,
    )
    if kind == "situation" and situation_id is not None:
        metrics.record_event(
            session,
            "situation_view",
            anon_id=anon_id,
            user_id=user_id,
            situation_id=situation_id,
            ref=ref,
            now=now,
        )


class AnalyticsMiddleware(BaseHTTPMiddleware):
    """Records page views. ``app.state.event_session`` is the session factory it writes with."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.method != "GET" or response.status_code != 200:
            return response
        if "text/html" not in response.headers.get("content-type", ""):
            return response
        found = page_kind(request.url.path)
        if found is None or metrics.looks_like_a_bot(request.headers.get("user-agent")):
            return response
        kind, slug = found

        anon_id = request.cookies.get(metrics.ANON_COOKIE)
        if not metrics.valid_anon_id(anon_id):
            anon_id = metrics.new_anon_id()
            set_anon_cookie(response, anon_id)
        factory: SessionFactory = request.app.state.event_session
        ref = request.query_params.get("ref")
        token = request.cookies.get(SESSION_COOKIE)

        def write() -> None:
            with factory() as session:
                record_page_view(
                    session,
                    kind=kind,
                    slug=slug,
                    anon_id=anon_id or "",
                    ref=ref,
                    session_token=token,
                    now=datetime.now(UTC),
                )

        try:
            await run_in_threadpool(write)
        except Exception:
            log.exception("could not record a page view")  # never breaks the page
        return response
