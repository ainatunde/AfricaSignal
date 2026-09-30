"""Turn place names found in text into places, without claiming more precision than the text
supports (spec B10).

Rules, in order:
1. Look up the normalised text among place aliases (exact match).
2. When several places match: keep the coarsest kind ("Lagos" is the state, not the LGA "Lagos
   Island"); among equals, use the context (states already found in the same claim, or the
   document's dominant state) to pick one; otherwise the answer is ``ambiguous``.
3. Only state names are matched fuzzily (rapidfuzz, score >= 92).
4. A neighbourhood resolves to the LGA that contains it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz, process
from sqlalchemy import text
from sqlalchemy.orm import Session

from africasignal.places.normalise import normalise, strip_qualifiers

Status = Literal["resolved", "ambiguous", "unknown"]
Precision = Literal["national", "state", "lga", "city", "unknown"]

FUZZY_CUTOFF = 92
# Coarser kinds win when one name matches several kinds.
_KIND_RANK = {"country": 0, "state": 1, "lga": 2, "neighbourhood_alias": 2, "city": 3}
_PRECISION = {
    "country": "national",
    "state": "state",
    "lga": "lga",
    "neighbourhood_alias": "lga",
    "city": "city",
}


@dataclass(frozen=True)
class Place:
    id: int
    kind: str
    name: str
    code: str
    parent_id: int | None
    state_id: int | None  # the containing state (itself, for a state)


@dataclass(frozen=True)
class Resolution:
    status: Status
    precision: Precision = "unknown"
    place: Place | None = None
    method: str = "none"  # exact | fuzzy | context | none
    candidates: tuple[Place, ...] = field(default=())

    @property
    def place_id(self) -> int | None:
        return self.place.id if self.place else None


_PLACE_COLUMNS = """
    p.id, p.kind::text AS kind, p.name, p.code, p.parent_id,
    CASE p.kind::text
        WHEN 'state' THEN p.id
        WHEN 'lga' THEN p.parent_id
        WHEN 'neighbourhood_alias' THEN (SELECT parent_id FROM place l WHERE l.id = p.parent_id)
        WHEN 'city' THEN (SELECT parent_id FROM place l WHERE l.id = p.parent_id)
    END AS state_id
"""


def _row_to_place(row: object) -> Place:
    r = row._mapping  # type: ignore[attr-defined]
    return Place(r["id"], r["kind"], r["name"], r["code"], r["parent_id"], r["state_id"])


def _lookup(session: Session, norm: str) -> list[Place]:
    rows = session.execute(
        text(
            f"SELECT DISTINCT {_PLACE_COLUMNS} FROM place_alias a "
            "JOIN place p ON p.id = a.place_id WHERE a.alias_norm = :n ORDER BY p.id"
        ),
        {"n": norm},
    ).all()
    return [_row_to_place(r) for r in rows]


def _state_names(session: Session) -> dict[str, Place]:
    """Normalised state name -> state, for fuzzy matching."""
    rows = session.execute(
        text(
            f"SELECT a.alias_norm, {_PLACE_COLUMNS} FROM place_alias a "
            "JOIN place p ON p.id = a.place_id WHERE p.kind = 'state'"
        )
    ).all()
    return {r._mapping["alias_norm"]: _row_to_place(r) for r in rows}


def _resolve_parent_lga(session: Session, place: Place) -> Place:
    """A neighbourhood is answered with the LGA that holds it."""
    if place.kind != "neighbourhood_alias" or place.parent_id is None:
        return place
    row = session.execute(
        text(f"SELECT {_PLACE_COLUMNS} FROM place p WHERE p.id = :id"), {"id": place.parent_id}
    ).one()
    return _row_to_place(row)


def _finish(session: Session, place: Place, method: str, candidates: list[Place]) -> Resolution:
    answer = _resolve_parent_lga(session, place)
    return Resolution(
        status="resolved",
        precision=_PRECISION[answer.kind],  # type: ignore[arg-type]
        place=answer,
        method=method,
        candidates=tuple(candidates),
    )


def resolve_place(
    session: Session,
    text_: str,
    *,
    context_state_ids: frozenset[int] = frozenset(),
) -> Resolution:
    """Resolve one place string. ``context_state_ids`` are states known from the same claim or the
    document, used only to break ties between places with the same name."""
    norm = normalise(text_)
    if not norm:
        return Resolution(status="unknown")

    candidates = _lookup(session, norm)
    if not candidates:
        stripped = strip_qualifiers(norm)
        if stripped and stripped != norm:
            candidates = _lookup(session, stripped)

    if candidates:
        best_rank = min(_KIND_RANK[c.kind] for c in candidates)
        coarsest = [c for c in candidates if _KIND_RANK[c.kind] == best_rank]
        if len(coarsest) == 1:
            return _finish(session, coarsest[0], "exact", candidates)
        in_context = [c for c in coarsest if c.state_id in context_state_ids]
        if len(in_context) == 1:
            return _finish(session, in_context[0], "context", candidates)
        return Resolution(status="ambiguous", candidates=tuple(candidates))

    # Fuzzy matching is limited to state names: a misspelt LGA is more likely a different LGA.
    states = _state_names(session)
    match = process.extractOne(
        strip_qualifiers(norm), list(states), scorer=fuzz.ratio, score_cutoff=FUZZY_CUTOFF
    )
    if match is not None:
        return _finish(session, states[match[0]], "fuzzy", [states[match[0]]])
    return Resolution(status="unknown")


def resolve_candidates(
    session: Session,
    candidates: list[str],
    *,
    document_state_id: int | None = None,
) -> Resolution:
    """The place a claim is about, from all place strings found in it.

    Unambiguous names are resolved first and their states become context for the rest. The
    answer is the most precise resolved place. If the resolved places lie in different states the
    claim is not about one place, so the answer is ``ambiguous``.
    """
    context: set[int] = {document_state_id} if document_state_id is not None else set()
    resolutions = [resolve_place(session, c) for c in candidates]
    for r in resolutions:
        if r.status == "resolved" and r.place and r.place.state_id and r.precision != "national":
            context.add(r.place.state_id)
    frozen = frozenset(context)
    resolutions = [
        r if r.status == "resolved" else resolve_place(session, c, context_state_ids=frozen)
        for r, c in zip(resolutions, candidates, strict=True)
    ]

    resolved = [r for r in resolutions if r.status == "resolved" and r.place is not None]
    if not resolved:
        ambiguous = [r for r in resolutions if r.status == "ambiguous"]
        return ambiguous[0] if ambiguous else Resolution(status="unknown")

    states = {
        r.place.state_id
        for r in resolved
        if r.place and r.place.state_id and r.precision != "national"
    }
    if len(states) > 1:
        return Resolution(
            status="ambiguous",
            candidates=tuple(r.place for r in resolved if r.place),
        )
    rank = {"national": 0, "state": 1, "lga": 2, "city": 3}
    return max(resolved, key=lambda r: rank[r.precision])


@dataclass(frozen=True)
class PointResolution:
    lga: Place
    state: Place


def resolve_point(session: Session, lat: float, lon: float) -> PointResolution | None:
    """The LGA and state containing a coordinate, or None if it lies outside Nigeria's LGAs.

    The coordinate is used for this lookup only and is never stored.
    """
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordinates out of range")
    row = session.execute(
        text(
            f"SELECT {_PLACE_COLUMNS} FROM place p WHERE p.kind = 'lga' "
            "AND ST_Covers(p.geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)) "
            "ORDER BY p.id LIMIT 1"
        ),
        {"lon": lon, "lat": lat},
    ).first()
    if row is None:
        return None
    lga = _row_to_place(row)
    state_row = session.execute(
        text(f"SELECT {_PLACE_COLUMNS} FROM place p WHERE p.id = :id"), {"id": lga.parent_id}
    ).one()
    return PointResolution(lga=lga, state=_row_to_place(state_row))
