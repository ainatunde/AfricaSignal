"""Read-only queries behind the public pages and the JSON API.

Everything the public sees is a *current published* assessment version: the one a situation's
``current_version_id`` points at, with status published, stale or withdrawn. Drafts and withheld
versions are never reachable from here, and superseded versions only through the history.
Nothing in this module writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from africasignal.catalog import load_items
from africasignal.models import (
    AssessmentVersion,
    EvidenceDocument,
    Measurement,
    Place,
    PlaceAlias,
    Series,
    Setting,
    Situation,
    Source,
    SourcePermission,
)
from africasignal.places.normalise import normalise

CURRENT_STATUSES = ("published", "stale", "withdrawn")  # what a current version can be
HISTORY_STATUSES = ("published", "stale", "superseded", "withdrawn")
EXCERPT_CAP = 300  # characters shown from a document even when its source allows more
CHART_PERIODS = 13
SEARCH_LIMIT = 10

_KIND_RANK = {"country": 0, "state": 1, "lga": 2, "city": 3, "neighbourhood_alias": 4}


# --- settings ----------------------------------------------------------------------------------


def publication_suspended(session: Session) -> bool:
    """The operator's kill switch (B11.5): pages stay up with a banner. Pages that do not depend
    on data (method, crawler) still render when the database is unreachable."""
    try:
        value = session.scalar(select(Setting.value).where(Setting.key == "publication_suspended"))
    except SQLAlchemyError:
        session.rollback()
        return False
    return value is True


# --- versions ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Current:
    situation: Situation
    version: AssessmentVersion
    place: Place

    @property
    def effective_status(self) -> str:
        return effective_status(self.version, datetime.now(UTC))


def effective_status(version: AssessmentVersion, now: datetime) -> str:
    """``stale`` once ``valid_until`` has passed, even if the hourly expiry job has not run yet."""
    if version.status == "published" and version.valid_until and version.valid_until < now:
        return "stale"
    return version.status


def _current_query() -> Any:
    return (
        select(Situation, AssessmentVersion, Place)
        .join(AssessmentVersion, AssessmentVersion.id == Situation.current_version_id)
        .join(Place, Place.id == Situation.place_id)
        .where(AssessmentVersion.status.in_(CURRENT_STATUSES), Situation.status != "closed")
    )


def current_situation(session: Session, slug: str) -> Current | None:
    row = session.execute(_current_query().where(Situation.slug == slug)).first()
    return Current(*row) if row else None


def current_situations(
    session: Session,
    *,
    place_ids: list[int] | None = None,
    topic: str | None = None,
    item_code: str | None = None,
    kind_of_place: str | None = None,
    published_after: datetime | None = None,
    material_only: bool = False,
    order: str = "newest",
    limit: int = 200,
    offset: int = 0,
) -> list[Current]:
    query = _current_query()
    if place_ids is not None:
        query = query.where(Situation.place_id.in_(place_ids))
    if topic:
        query = query.where(Situation.topic == topic)
    if item_code:
        query = query.where(Situation.item_code == item_code)
    if kind_of_place:
        query = query.where(Place.kind == kind_of_place)
    if published_after is not None:
        query = query.where(AssessmentVersion.published_at > published_after)
    if material_only:
        query = query.where(
            AssessmentVersion.severity != "none", AssessmentVersion.status == "published"
        )
    if order == "newest":
        query = query.order_by(AssessmentVersion.published_at.desc().nulls_last(), Situation.slug)
    else:
        query = query.order_by(Place.name, Situation.slug)
    return [Current(*row) for row in session.execute(query.limit(limit).offset(offset))]


def count_current_situations(
    session: Session, *, place_ids: list[int] | None = None, topic: str | None = None
) -> int:
    query = (
        select(func.count())
        .select_from(Situation)
        .join(AssessmentVersion, AssessmentVersion.id == Situation.current_version_id)
        .where(AssessmentVersion.status.in_(CURRENT_STATUSES), Situation.status != "closed")
    )
    if place_ids is not None:
        query = query.where(Situation.place_id.in_(place_ids))
    if topic:
        query = query.where(Situation.topic == topic)
    return int(session.scalar(query) or 0)


def public_versions(session: Session, situation_id: int) -> list[AssessmentVersion]:
    """Every version a reader may see, newest first. Drafts and withheld versions never appear."""
    return list(
        session.scalars(
            select(AssessmentVersion)
            .where(
                AssessmentVersion.situation_id == situation_id,
                AssessmentVersion.status.in_(HISTORY_STATUSES),
            )
            .order_by(AssessmentVersion.version.desc())
        )
    )


# --- evidence ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceView:
    document_id: int
    source_slug: str
    source_name: str
    title: str
    url: str
    date: datetime
    quote: str | None
    active: bool


def permitted_excerpt(text: str | None, permission: SourcePermission | None) -> str | None:
    """At most ``max_quote_chars`` of ``text``, cut at a word and marked with an ellipsis.

    No approved permission means no quote. A source with no limit is still capped at
    ``EXCERPT_CAP`` so a page never reproduces a whole article.
    """
    if not text or permission is None or permission.approved_at is None:
        return None
    limit = EXCERPT_CAP
    if permission.max_quote_chars is not None:
        limit = min(limit, permission.max_quote_chars)
    if limit <= 0:
        return None
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    cut = text[: limit - 1]
    if " " in cut and text[limit - 1] != " ":
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "…"


def _approved_permissions(session: Session, source_ids: set[int]) -> dict[int, SourcePermission]:
    """The newest approved permission version of each source."""
    newest: dict[int, SourcePermission] = {}
    if not source_ids:
        return newest
    rows = session.scalars(
        select(SourcePermission)
        .where(SourcePermission.source_id.in_(source_ids), SourcePermission.approved_at.isnot(None))
        .order_by(SourcePermission.version)
    )
    for permission in rows:
        newest[permission.source_id] = permission  # ascending order: the last one wins
    return newest


def evidence_ids(version: AssessmentVersion) -> list[int]:
    ids: list[int] = []
    for item in [*version.facts, *version.possible_factors]:
        for evidence_id in item.get("evidence_ids", []):
            if evidence_id not in ids:
                ids.append(evidence_id)
    return ids


def web_url(url: str) -> str:
    """``url`` when it is an http(s) link, else an empty string. Source addresses come from pages
    and feeds we do not control; a ``javascript:`` or ``data:`` address must never become a link."""
    return url if url.lower().startswith(("http://", "https://")) else ""


def evidence_for(session: Session, version: AssessmentVersion) -> list[EvidenceView]:
    """The documents a version rests on, newest first, each with the excerpt its source permits."""
    ids = evidence_ids(version)
    if not ids:
        return []
    rows = session.execute(
        select(EvidenceDocument, Source)
        .join(Source, Source.id == EvidenceDocument.source_id)
        .where(EvidenceDocument.id.in_(ids))
    ).all()
    permissions = _approved_permissions(session, {source.id for _, source in rows})
    views = [
        EvidenceView(
            document_id=doc.id,
            source_slug=source.slug,
            source_name=source.name,
            title=doc.title or source.name,
            url=web_url(doc.url),
            date=doc.published_at or doc.retrieved_at,
            quote=permitted_excerpt(doc.excerpt, permissions.get(source.id)),
            active=doc.status == "active",
        )
        for doc, source in rows
    ]
    return sorted(views, key=lambda v: (v.date, v.document_id), reverse=True)


# --- chart -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChartPoint:
    period_start: date
    value: Decimal


def chart_points(session: Session, situation: Situation) -> list[ChartPoint]:
    """The last 13 monthly values of the situation's item at its place (newest vintage wins when a
    month was restated; superseded values and withdrawn evidence are left out)."""
    if situation.kind != "price_series" or situation.item_code is None:
        return []
    rows = session.execute(
        select(Measurement.period_start, Measurement.value, Measurement.vintage)
        .join(Series, Series.id == Measurement.series_id)
        .join(EvidenceDocument, EvidenceDocument.id == Measurement.evidence_document_id)
        .where(
            Series.item_code == situation.item_code,
            Measurement.place_id == situation.place_id,
            Measurement.superseded_by_id.is_(None),
            EvidenceDocument.status == "active",
        )
    ).all()
    newest: dict[date, tuple[date, Decimal]] = {}
    for period_start, value, vintage in rows:
        if period_start not in newest or vintage > newest[period_start][0]:
            newest[period_start] = (vintage, Decimal(value))
    months = sorted(newest)[-CHART_PERIODS:]
    return [ChartPoint(m, newest[m][1]) for m in months]


# --- places ------------------------------------------------------------------------------------


def place_by_code(session: Session, code: str) -> Place | None:
    """The place with this code, ignoring case: state codes are upper case (``NG-LA``) but LGA and
    city codes end in a lower-case slug (``NG-LA-ikeja``), so upper-casing the input alone would
    never find them."""
    return session.scalars(select(Place).where(func.lower(Place.code) == code.lower())).first()


def ancestor_of_kind(session: Session, place: Place, kind: str) -> Place | None:
    """The place itself or the nearest ancestor of ``kind``."""
    current: Place | None = place
    for _ in range(6):
        if current is None:
            return None
        if current.kind == kind:
            return current
        current = session.get(Place, current.parent_id) if current.parent_id else None
    return None


def country(session: Session) -> Place | None:
    return session.scalars(select(Place).where(Place.kind == "country")).first()


def states(session: Session) -> list[Place]:
    return list(
        session.scalars(
            select(Place).where(Place.kind == "state", Place.code.isnot(None)).order_by(Place.name)
        )
    )


def place_summary(place: Place | None) -> dict[str, Any] | None:
    if place is None:
        return None
    return {"code": place.code, "name": place.name, "kind": place.kind}


def search_places(session: Session, q: str, limit: int = SEARCH_LIMIT) -> list[Place]:
    """Places whose name or alias starts with the text, or has a word that does. Exact matches
    first, then country, state, LGA, city. Only places with a code can be linked to."""
    norm = normalise(q)
    if len(norm) < 2:
        return []
    like = norm.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = session.execute(
        select(PlaceAlias.place_id, func.bool_or(PlaceAlias.alias_norm == norm))
        .join(Place, Place.id == PlaceAlias.place_id)
        .where(
            Place.code.isnot(None),
            Place.kind.in_(("country", "state", "lga", "city")),
            or_(
                PlaceAlias.alias_norm.like(f"{like}%", escape="\\"),
                PlaceAlias.alias_norm.like(f"% {like}%", escape="\\"),
            ),
        )
        .group_by(PlaceAlias.place_id)
    ).all()
    exact: dict[int, bool] = {place_id: bool(is_exact) for place_id, is_exact in rows}
    if not exact:
        return []
    places = session.scalars(select(Place).where(Place.id.in_(list(exact))))
    ranked = sorted(
        places,
        key=lambda p: (not exact[p.id], _KIND_RANK.get(p.kind, 9), -(p.population or 0), p.name),
    )
    return ranked[:limit]


def locate(session: Session, lat: float, lon: float) -> tuple[Place | None, Place | None]:
    """The LGA and state that contain a point. The point is used in this one query and nowhere
    else: it is not stored, logged or cached."""
    point = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
    lga = session.scalars(
        select(Place).where(Place.kind == "lga", func.ST_Covers(Place.geom, point)).limit(1)
    ).first()
    if lga is not None:
        return lga, ancestor_of_kind(session, lga, "state")
    state = session.scalars(
        select(Place).where(Place.kind == "state", func.ST_Covers(Place.geom, point)).limit(1)
    ).first()
    return None, state


# --- explore and coverage ----------------------------------------------------------------------


def fact_by_label(version: AssessmentVersion, label: str) -> dict[str, Any] | None:
    return next((f for f in version.facts if f.get("label") == label), None)


def item_rows(session: Session) -> list[dict[str, Any]]:
    """Every tracked item with the number of places that currently have a published situation."""
    count_rows = session.execute(
        select(Situation.item_code, func.count())
        .join(AssessmentVersion, AssessmentVersion.id == Situation.current_version_id)
        .where(
            AssessmentVersion.status.in_(CURRENT_STATUSES),
            Situation.kind == "price_series",
            Situation.item_code.isnot(None),
        )
        .group_by(Situation.item_code)
    ).all()
    counts: dict[str | None, int] = {code: int(n) for code, n in count_rows}
    return [
        {
            "code": item.code,
            "label": item.label,
            "topic": item.topic,
            "unit": item.unit,
            "situations": int(counts.get(item.code, 0)),
        }
        for item in load_items().items
    ]


def coverage(session: Session) -> dict[str, Any]:
    """Which items and places have data, the latest period per source, and source health."""
    latest = {
        item_code: (period_end, places)
        for item_code, period_end, places in session.execute(
            select(
                Series.item_code,
                func.max(Measurement.period_end),
                func.count(func.distinct(Measurement.place_id)),
            )
            .join(Measurement, Measurement.series_id == Series.id)
            .join(EvidenceDocument, EvidenceDocument.id == Measurement.evidence_document_id)
            .where(Measurement.superseded_by_id.is_(None), EvidenceDocument.status == "active")
            .group_by(Series.item_code)
        )
    }
    items = []
    for row in item_rows(session):
        period_end, places = latest.get(row["code"], (None, 0))
        items.append(
            {
                **row,
                "latest_period": period_end.strftime("%B %Y") if period_end else None,
                "latest_period_end": period_end.isoformat() if period_end else None,
                "places_with_data": int(places),
            }
        )
    period_rows = session.execute(
        select(Series.source_id, func.max(Measurement.period_end))
        .join(Measurement, Measurement.series_id == Series.id)
        .where(Measurement.superseded_by_id.is_(None))
        .group_by(Series.source_id)
    ).all()
    source_periods: dict[int, date] = {source_id: end for source_id, end in period_rows}
    sources = [
        {
            "slug": s.slug,
            "name": s.name,
            "kind": s.kind,
            "health": s.health,
            "last_success_at": s.last_success_at,
            "latest_period": (
                source_periods[s.id].strftime("%B %Y") if s.id in source_periods else None
            ),
            "coverage_note": s.coverage_note,
        }
        for s in session.scalars(
            select(Source).where(Source.active.is_(True)).order_by(Source.name)
        )
    ]
    return {"items": items, "sources": sources}
