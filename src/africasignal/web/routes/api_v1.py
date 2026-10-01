"""Read-only JSON API (AS-035, spec B11.4).

``GET /v1/situations?topic=&place=``, ``/v1/situations/{slug}``, ``/v1/situations/{slug}/versions``,
``/v1/places/search?q=`` and ``/v1/coverage``.

Only current published assessments are returned (drafts and withheld versions never are). Evidence
links carry an excerpt only as far as the source's approved permission allows, never more than its
``max_quote_chars``. Every route is limited to 60 requests a minute per client, counted in memory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from africasignal.models import AssessmentVersion, Place
from africasignal.web import queries
from africasignal.web.client_address import client_address
from africasignal.web.ratelimit import RateLimiter
from africasignal.web.session_dep import get_db

rate_limiter = RateLimiter(limit=60, window_seconds=60)
CACHE_SECONDS = 60
DEFAULT_LIMIT, MAX_LIMIT = 50, 200


def limit_requests(request: Request, response: Response) -> None:
    allowed, retry_after = rate_limiter.check(client_address(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests: the limit is 60 per minute.",
            headers={"Retry-After": str(retry_after)},
        )
    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"


Db = Annotated[Session, Depends(get_db)]
router = APIRouter(prefix="/v1", dependencies=[Depends(limit_requests)], tags=["v1"])


# --- response models ---------------------------------------------------------------------------


class PlaceOut(BaseModel):
    code: str | None
    name: str
    kind: str


class EvidenceOut(BaseModel):
    source: str
    source_name: str
    title: str
    url: str
    date: datetime
    quote: str | None
    withdrawn: bool


class SituationSummary(BaseModel):
    slug: str
    title: str
    kind: str
    topic: str
    item_code: str | None
    place: PlaceOut
    headline: str
    scope_label: str
    period_label: str
    evidence_state: str
    severity: str
    status: str
    version: int
    last_checked_at: datetime | None
    valid_until: datetime | None
    published_at: datetime | None
    url: str


class SituationDetail(SituationSummary):
    facts: list[dict[str, Any]]
    explanation: str | None
    possible_factors: list[dict[str, Any]]
    unknowns: list[str]
    change_summary: str | None
    evidence: list[EvidenceOut]


class SituationList(BaseModel):
    count: int
    limit: int
    offset: int
    situations: list[SituationSummary]


class VersionOut(BaseModel):
    version: int
    status: str
    headline: str
    scope_label: str
    period_label: str
    evidence_state: str
    severity: str
    change_summary: str | None
    published_at: datetime | None
    last_checked_at: datetime | None
    valid_until: datetime | None
    current: bool


class VersionList(BaseModel):
    slug: str
    versions: list[VersionOut]


class PlaceResult(BaseModel):
    code: str
    name: str
    kind: str
    parent: PlaceOut | None


class PlaceSearch(BaseModel):
    q: str
    places: list[PlaceResult]


class ItemCoverage(BaseModel):
    code: str
    label: str
    topic: str
    unit: str
    latest_period: str | None
    latest_period_end: str | None
    places_with_data: int
    situations: int


class SourceCoverage(BaseModel):
    slug: str
    name: str
    kind: str
    health: Literal["healthy", "degraded", "failing"]
    last_success_at: datetime | None
    latest_period: str | None
    coverage_note: str | None


class Coverage(BaseModel):
    items: list[ItemCoverage]
    sources: list[SourceCoverage]


# --- builders ----------------------------------------------------------------------------------


def _is_evidence_state_public(version: AssessmentVersion) -> bool:
    return version.evidence_state != "insufficient"


def summary_of(current: queries.Current) -> dict[str, Any]:
    v = current.version
    return {
        "slug": current.situation.slug,
        "title": current.situation.title,
        "kind": current.situation.kind,
        "topic": current.situation.topic,
        "item_code": current.situation.item_code,
        "place": queries.place_summary(current.place),
        "headline": f"Last known: {v.headline}"
        if current.effective_status == "stale"
        else v.headline,
        "scope_label": v.scope_label,
        "period_label": v.period_label,
        "evidence_state": v.evidence_state,
        "severity": v.severity,
        "status": current.effective_status,
        "version": v.version,
        "last_checked_at": v.last_checked_at,
        "valid_until": v.valid_until,
        "published_at": v.published_at,
        "url": f"/s/{current.situation.slug}",
    }


def evidence_out(views: list[queries.EvidenceView]) -> list[dict[str, Any]]:
    return [
        {
            "source": e.source_slug,
            "source_name": e.source_name,
            "title": e.title,
            "url": e.url,
            "date": e.date,
            "quote": e.quote,
            "withdrawn": not e.active,
        }
        for e in views
    ]


# --- routes ------------------------------------------------------------------------------------


@router.get("/situations", response_model=SituationList)
def list_situations(
    db: Db,
    topic: Literal["energy", "food"] | None = None,
    place: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    place_ids: list[int] | None = None
    if place:
        found = queries.place_by_code(db, place)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Unknown place code {place!r}.")
        # A finer place than a state answers with its state's situations.
        scope = queries.ancestor_of_kind(db, found, "state") or found
        place_ids = [scope.id]
    rows = queries.current_situations(
        db, place_ids=place_ids, topic=topic, limit=limit, offset=offset
    )
    total = queries.count_current_situations(db, place_ids=place_ids, topic=topic)
    return {
        "count": total,
        "limit": limit,
        "offset": offset,
        "situations": [summary_of(c) for c in rows],
    }


@router.get("/situations/{slug}", response_model=SituationDetail)
def get_situation(slug: str, db: Db) -> dict[str, Any]:
    current = queries.current_situation(db, slug)
    if current is None:
        raise HTTPException(status_code=404, detail="No published assessment for this situation.")
    v = current.version
    insufficient = not _is_evidence_state_public(v)
    return {
        **summary_of(current),
        "facts": v.facts,
        "explanation": None if insufficient else v.explanation,
        "possible_factors": v.possible_factors,
        "unknowns": v.unknowns,
        "change_summary": v.change_summary,
        "evidence": evidence_out(queries.evidence_for(db, v)),
    }


@router.get("/situations/{slug}/versions", response_model=VersionList)
def get_versions(slug: str, db: Db) -> dict[str, Any]:
    current = queries.current_situation(db, slug)
    if current is None:
        raise HTTPException(status_code=404, detail="No published assessment for this situation.")
    versions = queries.public_versions(db, current.situation.id)
    now = datetime.now(UTC)
    return {
        "slug": slug,
        "versions": [
            {
                "version": v.version,
                "status": queries.effective_status(v, now),
                "headline": f"Last known: {v.headline}"
                if queries.effective_status(v, now) == "stale"
                else v.headline,
                "scope_label": v.scope_label,
                "period_label": v.period_label,
                "evidence_state": v.evidence_state,
                "severity": v.severity,
                "change_summary": v.change_summary,
                "published_at": v.published_at,
                "last_checked_at": v.last_checked_at,
                "valid_until": v.valid_until,
                "current": v.id == current.situation.current_version_id,
            }
            for v in versions
        ],
    }


@router.get("/places/search", response_model=PlaceSearch)
def search_places(q: Annotated[str, Query(min_length=2, max_length=80)], db: Db) -> dict[str, Any]:
    results = []
    for place in queries.search_places(db, q):
        parent = db.get(Place, place.parent_id) if place.parent_id else None
        results.append(
            {
                "code": place.code,
                "name": place.name,
                "kind": place.kind,
                "parent": queries.place_summary(parent),
            }
        )
    return {"q": q, "places": results}


@router.get("/coverage", response_model=Coverage)
def get_coverage(db: Db) -> dict[str, Any]:
    return queries.coverage(db)
