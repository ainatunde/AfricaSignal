"""Public pages (AS-030, spec B11.1 to B11.3).

Every page works without JavaScript. The one place a visitor's choices are kept is first-party
cookies: ``place`` (the chosen place code) and ``visit_prev`` / ``visit_cur`` (for "since your last
visit"). No IP address is read, stored or logged here, and the "use my location" coordinates are
used inside a single query and then dropped.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from africasignal.config import get_settings
from africasignal.models import Place
from africasignal.net.netutil import USER_AGENT
from africasignal.web import queries
from africasignal.web.cache import TTLCache
from africasignal.web.chart import line_chart_svg
from africasignal.web.client_address import client_address
from africasignal.web.ratelimit import RateLimiter
from africasignal.web.render import (
    BADGES,
    EVIDENCE_STATE_HELP,
    badge,
    public_base_url,
    render,
    templates,
)
from africasignal.web.session_dep import get_db

router = APIRouter()
Db = Annotated[Session, Depends(get_db)]

PLACE_COOKIE = "place"
VISIT_PREV, VISIT_CUR = "visit_prev", "visit_cur"
SESSION_GAP = timedelta(minutes=30)
COOKIE_MAX_AGE = 365 * 24 * 3600
PAGE_CACHE_SECONDS = 300
TOPICS = ("energy", "food")
KIND_WORDS = {
    "country": "country",
    "state": "state",
    "lga": "local government area",
    "city": "city",
}

# "Use my location" runs a spatial query and needs no cookie or sign-in: limited per client.
locate_limiter = RateLimiter(limit=30, window_seconds=60)

# Rendered situation pages, keyed by version (B11.3). Nothing personal is ever in these pages.
_page_cache: TTLCache[str] = TTLCache(ttl_seconds=PAGE_CACHE_SECONDS)


def clear_page_cache() -> None:
    _page_cache.clear()


def _now() -> datetime:
    return datetime.now(UTC)


def _set_cookie(response: Response, name: str, value: str) -> None:
    response.set_cookie(
        name,
        value,
        max_age=COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=get_settings().env != "development",
    )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def visit_times(
    prev_cookie: str | None, cur_cookie: str | None, now: datetime
) -> tuple[datetime | None, datetime]:
    """(start of the previous visit, time to remember as this one's last request).

    A new visit starts after 30 minutes without a request, so "since your last visit" still shows
    the same items when the page is reloaded a minute later.
    """
    prev, cur = _parse_time(prev_cookie), _parse_time(cur_cookie)
    if cur is not None and now - cur > SESSION_GAP:
        prev = cur
    return prev, now


def _with_badges(items: list[queries.Current]) -> list[tuple[queries.Current, dict[str, str]]]:
    return [(c, badge(c.version)) for c in items]


def _not_found(request: Request, message: str | None = None) -> HTMLResponse:
    return render(request, "not_found.html", {"message": message, "nav": ""}, status_code=404)


def _ctx(db: Session, nav: str, **extra: Any) -> dict[str, Any]:
    return {"nav": nav, "suspended": queries.publication_suspended(db), **extra}


def _place_from_request(request: Request, db: Session) -> Any:
    code = request.cookies.get(PLACE_COOKIE)
    return queries.place_by_code(db, code) if code else None


def _picker_context(db: Session, q: str | None) -> dict[str, Any]:
    results = []
    if q:
        for place in queries.search_places(db, q):
            parent = db.get(Place, place.parent_id) if place.parent_id else None
            results.append(
                {
                    "code": place.code,
                    "name": place.name,
                    "kind": place.kind,
                    "parent_name": parent.name if parent and parent.kind != "country" else None,
                }
            )
    return {
        "q": q,
        "results": results,
        "state_list": queries.states(db),
        "kind_words": KIND_WORDS,
    }


# --- For You -----------------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    db: Db,
    place: Annotated[str | None, Query(max_length=32)] = None,
    q: Annotated[str | None, Query(max_length=80)] = None,
    change: Annotated[str | None, Query(max_length=1)] = None,
) -> Response:
    if place:
        chosen = queries.place_by_code(db, place)
        if chosen is not None:
            redirect = RedirectResponse("/", status_code=303)
            _set_cookie(redirect, PLACE_COOKIE, chosen.code or "")
            return redirect

    chosen = _place_from_request(request, db)
    now = _now()
    prev, cur = visit_times(request.cookies.get(VISIT_PREV), request.cookies.get(VISIT_CUR), now)
    picking = chosen is None or bool(change) or bool(q)
    context: dict[str, Any] = _ctx(db, "home", place=chosen, change=picking and chosen is not None)
    if picking:
        context.update(_picker_context(db, q))

    if chosen is not None:
        state = queries.ancestor_of_kind(db, chosen, "state")
        nation = queries.country(db)
        place_ids = [p.id for p in (state, nation) if p is not None]
        since_items = (
            _with_badges(queries.current_situations(db, place_ids=place_ids, published_after=prev))
            if prev is not None
            else []
        )
        latest = queries.current_situations(db, place_ids=place_ids, material_only=True, limit=10)
        heading = "Latest material changes"
        if not latest:
            latest = queries.current_situations(db, place_ids=place_ids, limit=10)
            heading = "Latest figures"
        context.update(
            state=state,
            since=prev,
            since_items=since_items,
            latest_items=_with_badges(latest),
            latest_heading=heading,
        )
    response = render(request, "home.html", context)
    if chosen is not None:
        if prev is not None:
            _set_cookie(response, VISIT_PREV, prev.isoformat())
        _set_cookie(response, VISIT_CUR, cur.isoformat())
    return response


class Coordinates(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


@router.post("/places/locate")
def locate_place(body: Coordinates, request: Request, db: Db) -> JSONResponse:
    """Turn a position into the LGA and state that contain it. Writes nothing and logs nothing:
    the coordinates live in this function's arguments and one query, then are gone."""
    allowed, retry_after = locate_limiter.check(client_address(request))
    if not allowed:
        return JSONResponse(
            {"detail": "Too many requests."},
            status_code=429,
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )
    lga, state = queries.locate(db, body.lat, body.lon)
    return JSONResponse(
        {"lga": queries.place_summary(lga), "state": queries.place_summary(state)},
        headers={"Cache-Control": "no-store"},
    )


# --- explore -----------------------------------------------------------------------------------


@router.get("/explore", response_class=HTMLResponse)
def explore(
    request: Request,
    db: Db,
    topic: Annotated[str, Query(max_length=10)] = "energy",
    item: Annotated[str | None, Query(max_length=40)] = None,
) -> Response:
    if topic not in TOPICS:
        topic = "energy"
    items = [i for i in queries.item_rows(db) if i["topic"] == topic]
    selected = next((i for i in items if i["code"] == item), None)
    context = _ctx(db, "explore", topic=topic, topics=TOPICS, items=items, selected=selected)
    if selected is not None:
        nationals = queries.current_situations(
            db, item_code=selected["code"], kind_of_place="country", limit=1
        )
        context["national"] = _with_badges(nationals)[0] if nationals else None
        rows = []
        for current in queries.current_situations(
            db, item_code=selected["code"], kind_of_place="state", order="place"
        ):
            current_fact = queries.fact_by_label(current.version, "Current price")
            rows.append(
                {
                    "slug": current.situation.slug,
                    "place": current.place.name,
                    "current": current_fact,
                    "mom": queries.fact_by_label(current.version, "Month-on-month change"),
                    "badge": badge(current.version),
                    "stale": current.effective_status == "stale",
                }
            )
        context["state_rows"] = rows
    return render(request, "explore.html", context, cache_seconds=60)


# --- situation pages ---------------------------------------------------------------------------


def _summary(version: Any) -> dict[str, Any] | None:
    current = queries.fact_by_label(version, "Current price")
    if current is None:
        return None
    return {
        "current": current,
        "mom": queries.fact_by_label(version, "Month-on-month change"),
        "yoy": queries.fact_by_label(version, "Year-on-year change"),
    }


@router.get("/s/{slug}", response_class=HTMLResponse)
def situation_page(request: Request, slug: str, db: Db) -> Response:
    current = queries.current_situation(db, slug)
    if current is None:
        return _not_found(request, "This situation has no published assessment.")
    suspended = queries.publication_suspended(db)
    status = current.effective_status
    base_url = public_base_url(db)
    key = (current.version.id, status, suspended, base_url)
    page = _page_cache.get(key)
    if page is None:
        version = current.version
        points = queries.chart_points(db, current.situation)
        unit = next((str(f["unit"]) for f in version.facts if f.get("unit") not in (None, "%")), "")
        context = {
            "nav": "",
            "suspended": suspended,
            "situation": current.situation,
            "version": version,
            "place": current.place,
            "status": status,
            "b": badge(version),
            "help": EVIDENCE_STATE_HELP[version.evidence_state],
            "summary": _summary(version) if version.evidence_state != "insufficient" else None,
            "facts": version.facts,
            "chart": line_chart_svg(points, unit, current.situation.title) if points else "",
            "chart_from": f"{points[0].period_start:%B %Y}" if points else "",
            "chart_to": f"{points[-1].period_start:%B %Y}" if points else "",
            "evidence": queries.evidence_for(db, version),
            "share_url": f"{base_url}/s/{slug}?ref=share",
        }
        page = templates.env.get_template("situation.html").render(context)
        _page_cache.set(key, page)
    return HTMLResponse(page, headers={"Cache-Control": f"public, max-age={PAGE_CACHE_SECONDS}"})


@router.get("/s/{slug}/history", response_class=HTMLResponse)
def situation_history(request: Request, slug: str, db: Db) -> Response:
    current = queries.current_situation(db, slug)
    if current is None:
        return _not_found(request, "This situation has no published assessment.")
    now = _now()
    versions = [
        (v, badge(v), queries.effective_status(v, now))
        for v in queries.public_versions(db, current.situation.id)
    ]
    context = _ctx(db, "", situation=current.situation, versions=versions)
    return render(request, "history.html", context, cache_seconds=60)


# --- places, coverage and about ----------------------------------------------------------------


@router.get("/places/{code}", response_class=HTMLResponse)
def place_page(request: Request, code: str, db: Db) -> Response:
    place = queries.place_by_code(db, code)
    if place is None:
        return _not_found(request, "We do not have that place.")
    # Finer places than a state show their state's figures, clearly labelled (B11.1).
    scope = queries.ancestor_of_kind(db, place, "state") or queries.ancestor_of_kind(
        db, place, "country"
    )
    nation = queries.country(db)
    local = queries.current_situations(db, place_ids=[scope.id]) if scope else []
    national = (
        queries.current_situations(db, place_ids=[nation.id])
        if nation and scope and scope.id != nation.id
        else []
    )
    context = _ctx(
        db,
        "",
        place=place,
        scope_place=scope,
        local_items=_with_badges(local),
        national_items=_with_badges(national),
    )
    return render(request, "place.html", context, cache_seconds=60)


@router.get("/coverage", response_class=HTMLResponse)
def coverage_page(request: Request, db: Db) -> Response:
    return render(
        request, "coverage.html", _ctx(db, "coverage", data=queries.coverage(db)), cache_seconds=60
    )


@router.get("/about/method", response_class=HTMLResponse)
def method_page(request: Request, db: Db) -> Response:
    context = _ctx(db, "method", badges=BADGES, help=EVIDENCE_STATE_HELP)
    return render(request, "method.html", context, cache_seconds=300)


@router.get("/about/bot", response_class=HTMLResponse)
def bot_page(request: Request, db: Db) -> Response:
    contact = os.environ.get("BOT_CONTACT_EMAIL", "")
    context = _ctx(db, "", user_agent=USER_AGENT, contact=contact)
    return render(request, "bot.html", context, cache_seconds=300)


# --- error pages -------------------------------------------------------------------------------


def register_error_pages(app: FastAPI) -> None:
    """HTML 404 pages for the site; the JSON API keeps its JSON errors."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404 and not request.url.path.startswith("/v1"):
            return templates.TemplateResponse(
                request, "not_found.html", {"nav": "", "message": None}, status_code=404
            )
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers or None
        )
