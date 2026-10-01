"""Accounts, follows and in-site notifications (AS-031, web half), and unsubscribe (AS-032).

Sign-in is a magic link. The email links to ``/signin/verify?token=…``, which only shows a button:
mail scanners open links before people do, so the token is used up by the POST behind the button,
never by the GET. A sign-in request always answers "check your email", whether or not the address
is known or limited, so the page reveals nothing about who has an account.

Unsubscribing works the same way: ``GET /unsubscribe`` shows a confirm button, ``POST`` does it. A
mail client's one-click unsubscribe (RFC 8058) is a POST to the same link and needs no page.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select, update
from starlette.responses import RedirectResponse

from africasignal import metrics
from africasignal.models import AssessmentVersion, Notification, Place, Preference, Situation
from africasignal.publish import accounts, login_tokens, tokens
from africasignal.publish.email_render import UNSUBSCRIBE_PURPOSE
from africasignal.web import queries
from africasignal.web.analytics import SESSION_COOKIE
from africasignal.web.client_address import client_address
from africasignal.web.ratelimit import RateLimiter
from africasignal.web.render import badge, render
from africasignal.web.user_dep import (
    NEXT_COOKIE,
    CurrentUser,
    Db,
    clear_session_cookie,
    now,
    private_headers,
    remember_next,
    safe_next,
    set_session_cookie,
    sign_in_redirect,
)

router = APIRouter()

# Sign-in requests per client per hour, on top of the per-address limit in ``login_tokens``.
signin_limiter = RateLimiter(limit=10, window_seconds=3600)

TOPICS = ("energy", "food")
NOTICES = {
    "followed": "You now follow this situation.",
    "unfollowed": "You no longer follow this situation.",
    "saved": "Saved.",
    "digest_on": "You will get the weekly email.",
    "digest_off": "You will not get the weekly email.",
    "read": "Marked as read.",
}
KIND_WORDS = {
    "new_version": "New information",
    "correction": "Correction",
    "withdrawal": "Withdrawn",
}
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,100}")


def _client(request: Request) -> str:
    return client_address(request)


def _page(
    request: Request, db: Db, name: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    context = {"nav": "", "suspended": queries.publication_suspended(db), **context}
    context.setdefault("notice", NOTICES.get(request.query_params.get("notice", "")))
    return private_headers(render(request, name, context, status_code=status_code))


# --- sign in ------------------------------------------------------------------------------------


@router.get("/signin", response_class=HTMLResponse)
def signin_form(request: Request, user: CurrentUser, db: Db) -> Response:
    if user is not None:
        return RedirectResponse("/account", status_code=303)
    return _page(request, db, "signin.html", {"sent": False, "error": None, "email": ""})


@router.post("/signin", response_class=HTMLResponse)
def signin_request(
    request: Request,
    db: Db,
    email: Annotated[str, Form(max_length=320)] = "",
    next: Annotated[str, Form(max_length=300)] = "",
) -> Response:
    allowed, retry_after = signin_limiter.check(_client(request))
    if not allowed:
        response = _page(
            request,
            db,
            "signin.html",
            {"sent": False, "error": "Too many requests. Please try again later.", "email": ""},
            429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
    if login_tokens.normalise_email(email) is None:
        return _page(
            request,
            db,
            "signin.html",
            {"sent": False, "error": "Enter a valid email address.", "email": email},
            400,
        )
    login_tokens.request_login(db, email, now())  # True or False, the answer is the same
    db.commit()
    response = _page(request, db, "signin.html", {"sent": True, "error": None, "email": ""})
    remember_next(response, next)
    return response


@router.get("/signin/verify", response_class=HTMLResponse)
def signin_confirm(
    request: Request, db: Db, token: Annotated[str, Query(max_length=200)] = ""
) -> Response:
    """Shows a button. The token is not used here: a mail scanner must not spend it."""
    if not _TOKEN_RE.fullmatch(token):
        return _page(
            request,
            db,
            "signin.html",
            {"sent": False, "email": "", "error": "That sign-in link is not valid."},
            400,
        )
    response = _page(request, db, "signin_confirm.html", {"token": token})
    return private_headers(response, sensitive=True)


@router.post("/signin/verify", response_class=HTMLResponse)
def signin_verify(
    request: Request, db: Db, token: Annotated[str, Form(max_length=200)] = ""
) -> Response:
    signed_in = login_tokens.consume_login_token(db, token, now()) if token else None
    if signed_in is None:
        return _page(
            request,
            db,
            "signin.html",
            {
                "sent": False,
                "email": "",
                "error": "That sign-in link has expired or was already used. Ask for a new one.",
            },
            400,
        )
    db.commit()
    target = safe_next(request.cookies.get(NEXT_COOKIE)) or "/account"
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, signed_in.session_token)
    response.delete_cookie(NEXT_COOKIE, path="/")
    return private_headers(response, sensitive=True)


@router.post("/signout")
def signout(request: Request, db: Db) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        login_tokens.revoke_session(db, token, now())
        db.commit()
    response = RedirectResponse("/", status_code=303)
    clear_session_cookie(response)
    return response


# --- follows ------------------------------------------------------------------------------------


def _followable(db: Db, slug: str) -> Situation | None:
    current = queries.current_situation(db, slug)
    return current.situation if current else None


@router.post("/s/{slug}/follow")
def follow_situation(slug: str, request: Request, user: CurrentUser, db: Db) -> Response:
    if user is None:
        return sign_in_redirect(f"/s/{slug}")
    situation = _followable(db, slug)
    if situation is None:
        return _not_found(request, db)
    if accounts.follow(db, user.id, situation.id):
        metrics.record_event(
            db,
            "follow",
            anon_id=request.cookies.get(metrics.ANON_COOKIE),
            user_id=user.id,
            situation_id=situation.id,
            now=now(),
        )
    db.commit()
    return RedirectResponse("/following?notice=followed", status_code=303)


@router.post("/s/{slug}/unfollow")
def unfollow_situation(slug: str, request: Request, user: CurrentUser, db: Db) -> Response:
    if user is None:
        return sign_in_redirect("/following")
    situation = db.scalars(select(Situation).where(Situation.slug == slug)).one_or_none()
    if situation is None:
        return _not_found(request, db)
    if accounts.unfollow(db, user.id, situation.id):
        metrics.record_event(
            db,
            "unfollow",
            anon_id=request.cookies.get(metrics.ANON_COOKIE),
            user_id=user.id,
            situation_id=situation.id,
            now=now(),
        )
    db.commit()
    return RedirectResponse("/following?notice=unfollowed", status_code=303)


def _not_found(request: Request, db: Db) -> Response:
    return _page(
        request,
        db,
        "not_found.html",
        {"message": "This situation has no published assessment."},
        404,
    )


def unread_count(db: Db, user_id: int) -> int:
    return len(
        db.scalars(
            select(Notification.id).where(
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
                Notification.cancelled_at.is_(None),
            )
        ).all()
    )


@router.get("/following", response_class=HTMLResponse)
def following(request: Request, user: CurrentUser, db: Db) -> Response:
    if user is None:
        return sign_in_redirect("/following")
    items = []
    for situation in accounts.followed_situations(db, user.id):
        current = queries.current_situation(db, situation.slug)
        items.append(
            {
                "situation": situation,
                "current": current,
                "badge": badge(current.version) if current else None,
            }
        )
    updates = db.execute(
        select(Notification, AssessmentVersion, Situation)
        .join(AssessmentVersion, AssessmentVersion.id == Notification.assessment_version_id)
        .join(Situation, Situation.id == AssessmentVersion.situation_id)
        .where(
            Notification.user_id == user.id,
            Notification.read_at.is_(None),
            Notification.cancelled_at.is_(None),
        )
        .order_by(Notification.id.desc())
        .limit(50)
    ).all()
    return _page(
        request,
        db,
        "following.html",
        {"items": items, "updates": updates, "kind_words": KIND_WORDS, "user": user},
    )


@router.post("/following/read")
def mark_read(user: CurrentUser, db: Db) -> Response:
    if user is None:
        return sign_in_redirect("/following")
    db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=now())
    )
    db.commit()
    return RedirectResponse("/following?notice=read", status_code=303)


# --- account ------------------------------------------------------------------------------------


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user: CurrentUser, db: Db) -> Response:
    if user is None:
        return sign_in_redirect("/account")
    preference = db.get(Preference, user.id)
    chosen_ids = set(preference.place_ids) if preference else set()
    chosen_topics = set(preference.topics) if preference else set()
    states = [(s, s.id in chosen_ids) for s in queries.states(db)]
    return _page(
        request,
        db,
        "account.html",
        {
            "user": user,
            "states": states,
            "topics": [(t, t in chosen_topics) for t in TOPICS],
        },
    )


@router.post("/account/digest")
def account_digest(user: CurrentUser, db: Db, digest: Annotated[str, Form()] = "") -> Response:
    if user is None:
        return sign_in_redirect("/account")
    on = digest == "on"
    accounts.set_digest_opt_in(db, user.id, on, now())
    db.commit()
    return RedirectResponse(f"/account?notice={'digest_on' if on else 'digest_off'}", 303)


@router.post("/account/preferences")
async def account_preferences(request: Request, user: CurrentUser, db: Db) -> Response:
    if user is None:
        return sign_in_redirect("/account")
    form = await request.form()
    codes = [str(v) for v in form.getlist("place") if isinstance(v, str)][:40]
    topics = [str(v) for v in form.getlist("topic") if v in TOPICS]
    place_ids = [
        pid
        for pid in db.scalars(
            select(Place.id).where(Place.code.in_(codes), Place.kind.in_(("state", "country")))
        )
    ]
    accounts.set_preferences(db, user.id, sorted(place_ids), sorted(set(topics)))
    db.commit()
    return RedirectResponse("/account?notice=saved", status_code=303)


@router.get("/account/export")
def account_export(user: CurrentUser, db: Db) -> Response:
    if user is None:
        return sign_in_redirect("/account")
    data = accounts.export_account(db, user.id)
    response = Response(
        json.dumps(data, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="africasignal-my-data.json"'},
    )
    return private_headers(response, sensitive=True)


@router.post("/account/delete", response_class=HTMLResponse)
def account_delete(
    request: Request, user: CurrentUser, db: Db, confirm: Annotated[str, Form()] = ""
) -> Response:
    if user is None:
        return sign_in_redirect("/account")
    if confirm.strip().lower() != "delete":
        return _page(
            request,
            db,
            "account.html",
            {
                "user": user,
                "states": [(s, False) for s in queries.states(db)],
                "topics": [(t, False) for t in TOPICS],
                "error": "To delete your account, type the word delete in the box.",
            },
            400,
        )
    accounts.delete_account(db, user.id, now())
    db.commit()
    response = _page(request, db, "account_deleted.html", {})
    clear_session_cookie(response)
    return response


# --- unsubscribe --------------------------------------------------------------------------------


def _unsubscribe_value(token: str) -> bool:
    value = tokens.verify(UNSUBSCRIBE_PURPOSE, token)
    return value is not None and value.isdigit()


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_confirm(
    request: Request, db: Db, t: Annotated[str, Query(max_length=200)] = ""
) -> Response:
    """Shows a button; a mail scanner opening the link must not unsubscribe anyone."""
    if not _unsubscribe_value(t):
        return _page(request, db, "unsubscribe.html", {"token": None, "done": False}, 400)
    return private_headers(
        _page(request, db, "unsubscribe.html", {"token": t, "done": False}), sensitive=True
    )


@router.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(
    request: Request,
    db: Db,
    t: Annotated[str, Query(max_length=200)] = "",
    form_t: Annotated[str, Form(alias="t", max_length=200)] = "",
) -> Response:
    """The button on the page, and a mail client's one-click unsubscribe (the token is in the
    query string of the link it posts to)."""
    token = t or form_t
    if not accounts.unsubscribe(db, token):
        return _page(request, db, "unsubscribe.html", {"token": None, "done": False}, 400)
    db.commit()
    return private_headers(
        _page(request, db, "unsubscribe.html", {"token": None, "done": True}), sensitive=True
    )
