"""Feedback and the small event endpoints (AS-034).

* ``POST /s/{slug}/useful``: "Was this useful?" yes or no. One answer per visitor per version; a
  second answer replaces the first.
* ``GET/POST /s/{slug}/report``: "Report an error", with free text and an optional contact email
  that is stored only if the reader types it.
* ``POST /s/{slug}/share``: a beacon the "Share this page" button sends when it copies the link.
* ``GET /e/o/{token}.gif``: the digest's tracking pixel. Digests go only to readers who opted in.

Feedback is limited to 10 submissions per visitor (``anon_id``) per 24 hours, counted in the
database. No IP address is stored; the in-memory limiter below keys on a salted hash that lives in
process memory only, and is there for the one event endpoint that has no ``anon_id`` to count.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from starlette.responses import RedirectResponse

from africasignal import metrics
from africasignal.models import AppUser, Event, Feedback
from africasignal.publish import login_tokens, tokens
from africasignal.publish.email_render import DIGEST_OPEN_PURPOSE
from africasignal.web import queries
from africasignal.web.analytics import set_anon_cookie
from africasignal.web.ratelimit import RateLimiter
from africasignal.web.render import render
from africasignal.web.user_dep import CurrentUser, Db, now, private_headers

router = APIRouter()

MAX_FEEDBACK_PER_DAY = 10
MAX_TEXT_CHARS = 2000
MAX_CONTACT_CHARS = 254

share_limiter = RateLimiter(limit=30, window_seconds=60)
# Feedback is also limited per visitor code (below), but a script can drop its cookie and get a
# new code on every request, so a second limit keys on the client. It is generous, because readers
# behind one shared address (an office, a mobile carrier) must not lock each other out.
feedback_limiter = RateLimiter(limit=30, window_seconds=3600)

# A 1x1 transparent GIF.
_PIXEL = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
)


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _visitor(request: Request) -> tuple[str, bool]:
    """(the visitor's anon_id, whether it is new and the response must set the cookie)."""
    existing = request.cookies.get(metrics.ANON_COOKIE)
    if metrics.valid_anon_id(existing):
        return existing or "", False
    return metrics.new_anon_id(), True


def _finish(response: Response, anon_id: str, is_new: bool) -> Response:
    if is_new:
        set_anon_cookie(response, anon_id)
    return response


def _page(
    request: Request, db: Db, name: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    context = {"nav": "", "suspended": queries.publication_suspended(db), **context}
    return private_headers(render(request, name, context, status_code=status_code))


def _situation_or_404(request: Request, db: Db, slug: str) -> queries.Current | Response:
    current = queries.current_situation(db, slug)
    if current is None:
        return _page(
            request,
            db,
            "not_found.html",
            {"message": "This situation has no published assessment."},
            404,
        )
    return current


def submissions_today(db: Db, anon_id: str, at: datetime) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Feedback)
            .where(Feedback.anon_id == anon_id, Feedback.created_at > at - timedelta(days=1))
        )
        or 0
    )


def _too_many(request: Request, db: Db, current: queries.Current) -> Response:
    return _page(
        request,
        db,
        "report.html",
        {
            "situation": current.situation,
            "text": "",
            "contact": "",
            "error": "You have sent a lot of feedback today. Please try again tomorrow.",
        },
        429,
    )


def _save(
    db: Db,
    request: Request,
    user: Any,
    current: queries.Current,
    anon_id: str,
    kind: Literal["useful_yes", "useful_no", "error_report"],
    *,
    text: str | None = None,
    contact: str | None = None,
) -> None:
    at = now()
    db.add(
        Feedback(
            assessment_version_id=current.version.id,
            user_id=user.id if user else None,
            anon_id=anon_id,
            kind=kind,
            text=text,
            contact_email=contact,
        )
    )
    metrics.record_event(
        db,
        "feedback",
        anon_id=anon_id,
        user_id=user.id if user else None,
        situation_id=current.situation.id,
        props={"kind": kind},
        now=at,
    )


@router.post("/s/{slug}/useful")
def useful(
    slug: str,
    request: Request,
    user: CurrentUser,
    db: Db,
    answer: Annotated[str, Form(max_length=3)] = "",
) -> Response:
    current = _situation_or_404(request, db, slug)
    if isinstance(current, Response):
        return current
    if answer not in ("yes", "no"):
        return RedirectResponse(f"/s/{slug}", status_code=303)
    anon_id, is_new = _visitor(request)
    if not feedback_limiter.check(_client(request))[0]:
        return _too_many(request, db, current)
    kind: Literal["useful_yes", "useful_no"] = "useful_yes" if answer == "yes" else "useful_no"
    earlier = db.scalars(
        select(Feedback).where(
            Feedback.anon_id == anon_id,
            Feedback.assessment_version_id == current.version.id,
            Feedback.kind.in_(("useful_yes", "useful_no")),
        )
    ).first()
    if earlier is not None:
        earlier.kind = kind  # a changed mind is not a second vote
    else:
        if submissions_today(db, anon_id, now()) >= MAX_FEEDBACK_PER_DAY:
            return _too_many(request, db, current)
        _save(db, request, user, current, anon_id, kind)
    db.commit()
    return _finish(RedirectResponse(f"/s/{slug}/thanks?k=useful", status_code=303), anon_id, is_new)


@router.get("/s/{slug}/report", response_class=HTMLResponse)
def report_form(slug: str, request: Request, db: Db) -> Response:
    current = _situation_or_404(request, db, slug)
    if isinstance(current, Response):
        return current
    return _page(
        request,
        db,
        "report.html",
        {"situation": current.situation, "text": "", "contact": "", "error": None},
    )


@router.post("/s/{slug}/report", response_class=HTMLResponse)
def report_submit(
    slug: str,
    request: Request,
    user: CurrentUser,
    db: Db,
    text: Annotated[str, Form()] = "",
    contact: Annotated[str, Form()] = "",
    website: Annotated[str, Form()] = "",
) -> Response:
    current = _situation_or_404(request, db, slug)
    if isinstance(current, Response):
        return current
    anon_id, is_new = _visitor(request)
    if not feedback_limiter.check(_client(request))[0]:
        return _finish(_too_many(request, db, current), anon_id, is_new)
    if website:  # a field people cannot see: only a script fills it in
        return _finish(
            RedirectResponse(f"/s/{slug}/thanks?k=report", status_code=303), anon_id, is_new
        )
    text, contact = text.strip(), contact.strip()
    error = None
    if not text:
        error = "Please tell us what looks wrong."
    elif len(text) > MAX_TEXT_CHARS:
        error = f"Please keep it under {MAX_TEXT_CHARS} characters."
    elif contact and (
        len(contact) > MAX_CONTACT_CHARS or login_tokens.normalise_email(contact) is None
    ):
        error = "That does not look like an email address. Leave it empty if you prefer."
    if error:
        response = _page(
            request,
            db,
            "report.html",
            {"situation": current.situation, "text": text, "contact": contact, "error": error},
            400,
        )
        return _finish(response, anon_id, is_new)
    if submissions_today(db, anon_id, now()) >= MAX_FEEDBACK_PER_DAY:
        return _finish(_too_many(request, db, current), anon_id, is_new)
    _save(
        db,
        request,
        user,
        current,
        anon_id,
        "error_report",
        text=text,
        contact=login_tokens.normalise_email(contact) if contact else None,
    )
    db.commit()
    return _finish(RedirectResponse(f"/s/{slug}/thanks?k=report", status_code=303), anon_id, is_new)


@router.get("/s/{slug}/thanks", response_class=HTMLResponse)
def thanks(slug: str, request: Request, db: Db, k: str = "") -> Response:
    current = _situation_or_404(request, db, slug)
    if isinstance(current, Response):
        return current
    return _page(
        request,
        db,
        "thanks.html",
        {"situation": current.situation, "kind": k if k in ("useful", "report") else "useful"},
    )


@router.post("/s/{slug}/share")
def share_click(slug: str, request: Request, user: CurrentUser, db: Db) -> Response:
    """The Share button's beacon. It answers 204 whatever happens, since the page does not wait."""
    allowed, _ = share_limiter.check(_client(request))
    current = queries.current_situation(db, slug) if allowed else None
    if current is not None:
        anon_id, _new = _visitor(request)
        metrics.record_event(
            db,
            "share_click",
            anon_id=anon_id if not _new else None,
            user_id=user.id if user else None,
            situation_id=current.situation.id,
            now=now(),
        )
        db.commit()
    return Response(status_code=204)


@router.get("/e/o/{token}.gif")
def digest_open(token: str, db: Db) -> Response:
    """Counts one open of a digest per reader per week, then returns the pixel."""
    response = Response(
        _PIXEL,
        media_type="image/gif",
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )
    value = tokens.verify(DIGEST_OPEN_PURPOSE, token)
    if value is None or ":" not in value:
        return response
    user_id_text, week = value.split(":", 1)
    if not user_id_text.isdigit():
        return response
    user_id = int(user_id_text)
    user = db.get(AppUser, user_id)
    if user is None or user.deleted_at is not None or not user.digest_opt_in:
        return response  # only readers who opted in are tracked
    seen = db.scalar(
        select(Event.id).where(
            Event.name == "digest_open",
            Event.user_id == user_id,
            Event.props["week"].as_string() == week,
        )
    )
    if seen is None:
        metrics.record_event(
            db, "digest_open", user_id=user_id, ref="email", props={"week": week}, now=now()
        )
        db.commit()
    return response
