"""Legal pages (AS-043, plan D3): privacy notice, terms, and the correction and takedown policy.

The text describes what this code does. Every number and cookie name on the privacy page comes from
the constant the code itself uses (retention, token lifetimes, cookie names), so the page cannot
drift from the behaviour without a test failing. The wording is a starting draft for a lawyer: each
page carries a banner saying so until an admin sets "Legal pages reviewed" to yes in the console
Settings. Who runs the site and the contact address also come from Settings; until they are set the
pages say so in plain view rather than showing a made-up name.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from africasignal import metrics, settings_store
from africasignal.net.netutil import USER_AGENT
from africasignal.publish import deletions, retention
from africasignal.publish.login_tokens import IDLE_LIMIT, LOGIN_TOKEN_TTL, SESSION_TTL
from africasignal.web import queries
from africasignal.web.analytics import SESSION_COOKIE
from africasignal.web.render import render
from africasignal.web.routes.public import COOKIE_MAX_AGE, PLACE_COOKIE, VISIT_CUR, VISIT_PREV
from africasignal.web.session_dep import get_db
from africasignal.web.user_dep import NEXT_COOKIE, NEXT_COOKIE_SECONDS

router = APIRouter()
Db = Annotated[Session, Depends(get_db)]

# Bump when the wording changes in a way readers should know about. Shown as "Draft of ..." until
# the pages are confirmed as reviewed, and as "Last changed" afterwards.
LEGAL_TEXT_DATE = "30 September 2026"
ERROR_REPORT_RESPONSE_HOURS = 72  # plan D3: correction and takedown response target
PRIVACY_REQUEST_DAYS = 30  # draft target for access, export and deletion help; counsel to confirm


def duration_words(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 86400 == 0:
        days = seconds // 86400
        if days >= 365 and days % 365 == 0:
            years = days // 365
            return f"{years} year" + ("s" if years != 1 else "")
        return f"{days} day" + ("s" if days != 1 else "")
    minutes = seconds // 60
    return f"{minutes} minutes"


def cookies() -> list[dict[str, str]]:
    """Every cookie the public site sets, from the constants that set them. The operator console's
    own cookie is not listed: only operators ever receive it."""
    return [
        {
            "name": PLACE_COOKIE,
            "purpose": "Remembers the state or area you picked, so the home page shows your place.",
            "lasts": duration_words(timedelta(seconds=COOKIE_MAX_AGE)),
            "set_when": "You pick a place.",
        },
        {
            "name": VISIT_PREV,
            "purpose": "The start of your previous visit, for “since your last visit”.",
            "lasts": duration_words(timedelta(seconds=COOKIE_MAX_AGE)),
            "set_when": "You have picked a place and open the home page.",
        },
        {
            "name": VISIT_CUR,
            "purpose": "The time of your latest page request, to tell when a new visit starts.",
            "lasts": duration_words(timedelta(seconds=COOKIE_MAX_AGE)),
            "set_when": "You have picked a place and open the home page.",
        },
        {
            "name": metrics.ANON_COOKIE,
            "purpose": "A random code, not derived from anything about you, that lets us count "
            "visitors and returning visitors, and limit feedback to 10 submissions a day.",
            "lasts": duration_words(timedelta(seconds=metrics.ANON_COOKIE_MAX_AGE)),
            "set_when": "You open a page or send feedback.",
        },
        {
            "name": SESSION_COOKIE,
            "purpose": "Keeps you signed in.",
            "lasts": duration_words(SESSION_TTL),
            "set_when": "You sign in.",
        },
        {
            "name": NEXT_COOKIE,
            "purpose": "Remembers which page you were heading to while you sign in.",
            "lasts": duration_words(timedelta(seconds=NEXT_COOKIE_SECONDS)),
            "set_when": "You ask to sign in from a page that needs an account.",
        },
    ]


def _ctx(db: Session, **extra: Any) -> dict[str, Any]:
    reviewed = settings_store.get(db, "legal_review_confirmed") == "yes"
    return {
        "nav": "",
        "suspended": queries.publication_suspended(db),
        "operator_name": settings_store.get(db, "operator_name"),
        "contact_email": settings_store.get(db, "contact_email"),
        "reviewed": reviewed,
        "text_date": LEGAL_TEXT_DATE,
        "response_hours": ERROR_REPORT_RESPONSE_HOURS,
        **extra,
    }


def _legal_page(request: Request, name: str, context: dict[str, Any]) -> Response:
    return render(request, name, context, cache_seconds=300)


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request, db: Db) -> Response:
    retention_days = settings_store.get_int(db, "backup_retain_days")
    context = _ctx(
        db,
        cookies=cookies(),
        anon_cookie=metrics.ANON_COOKIE,
        event_retention=f"{metrics.RETENTION.days // 31} months",
        login_link_minutes=int(LOGIN_TOKEN_TTL.total_seconds() // 60),
        session_days=SESSION_TTL.days,
        idle_days=IDLE_LIMIT.days,
        backup_days=retention_days,
        ledger_days=(timedelta(days=retention_days or 30) + deletions.LEDGER_MARGIN).days,
        feedback_months=retention.feedback_retention_months(db),
        unverified_days=retention.UNVERIFIED_ACCOUNT_TTL.days,
        login_record_days=retention.TOKEN_GRACE.days,
        email_provider=settings_store.get(db, "email_provider"),
        request_days=PRIVACY_REQUEST_DAYS,
        feedback_limit=10,
    )
    return _legal_page(request, "privacy.html", context)


@router.get("/terms", response_class=HTMLResponse)
def terms(request: Request, db: Db) -> Response:
    return _legal_page(request, "terms.html", _ctx(db, user_agent=USER_AGENT))


@router.get("/corrections", response_class=HTMLResponse)
def corrections(request: Request, db: Db) -> Response:
    return _legal_page(request, "corrections.html", _ctx(db))
