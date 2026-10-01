"""Situations and assessment versions for T1 price changes (spec B8.1, B8.2, AS-011).

``ensure_situations`` creates one situation per (item, place) that has two consecutive months of
current measurements. ``assess_situation`` loads the inputs, runs the pure computation in
``assess.price_change`` and stores a new version unless the inputs are unchanged.

Not here yet: the publication policy (AS-012). New versions are stored as ``draft`` with
``policy_version = 'unapplied'``, and ``situation.current_version_id`` stays empty until the
policy publishes one. Invalidation and corrections (AS-013) also build on the ``assessment_input``
rows written here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.assess.price_change import (
    TEMPLATE_VERSION,
    FactorSpec,
    PriceAssessment,
    PriceInputs,
    PricePoint,
    StatePoint,
    compute_price_change,
    place_phrase,
)
from africasignal.catalog import Item, load_items
from africasignal.jobs import queue
from africasignal.models import (
    AssessmentInput,
    AssessmentVersion,
    EvidenceDocument,
    Measurement,
    Place,
    ReportingOrigin,
    Series,
    Situation,
    Source,
)

log = logging.getLogger("africasignal.publish.situations")

POLICY_UNAPPLIED = "unapplied"  # until AS-012 decides each version
SCOPE_KINDS = ("country", "state")
Outcome = Literal["created", "unchanged", "skipped"]


@dataclass(frozen=True)
class AssessmentOutcome:
    outcome: Outcome
    version: AssessmentVersion | None = None
    reason: str | None = None


def slug_for(item_code: str, place_code: str) -> str:
    """``price-pms_litre-ng-la``."""
    return f"price-{item_code}-{place_code.lower()}"


def source_short_name(source: Source) -> str:
    """ "NBS" from "National Bureau of Statistics"; the full name when it is a single word."""
    words = (source.owner or source.name).replace("(", " ").replace(")", " ").split()
    acronym = "".join(w[0] for w in words if w[0].isupper())
    return acronym if len(acronym) >= 2 else (source.owner or source.name)


def _add_months(d: date, n: int) -> date:
    index = d.year * 12 + (d.month - 1) + n
    return date(index // 12, index % 12 + 1, 1)


@dataclass(frozen=True)
class _Row:
    measurement: Measurement
    source_label: str
    source: Source


def _current_rows(
    session: Session,
    item_code: str,
    place_ids: Iterable[int],
    periods: Iterable[date] | None = None,
) -> list[_Row]:
    """Current measurements (not superseded, evidence still active) of an item at places. When
    several sources report the same month, the newest vintage wins."""
    query = (
        select(Measurement, EvidenceDocument, ReportingOrigin.label, Source)
        .join(Series, Series.id == Measurement.series_id)
        .join(EvidenceDocument, EvidenceDocument.id == Measurement.evidence_document_id)
        .join(Source, Source.id == EvidenceDocument.source_id)
        .outerjoin(ReportingOrigin, ReportingOrigin.id == EvidenceDocument.origin_id)
        .where(
            Series.item_code == item_code,
            Measurement.place_id.in_(list(place_ids)),
            Measurement.superseded_by_id.is_(None),
            EvidenceDocument.status == "active",
        )
    )
    if periods is not None:
        query = query.where(Measurement.period_start.in_(list(periods)))
    newest: dict[tuple[int, date], _Row] = {}
    for measurement, document, origin_label, source in session.execute(query):
        row = _Row(measurement, origin_label or document.title or source.name, source)
        key = (measurement.place_id, measurement.period_start)
        if key not in newest or measurement.vintage > newest[key].measurement.vintage:
            newest[key] = row
    return list(newest.values())


def _point(row: _Row) -> PricePoint:
    m = row.measurement
    return PricePoint(
        measurement_id=m.id,
        period_start=m.period_start,
        period_end=m.period_end,
        value=Decimal(m.value),
        evidence_document_id=m.evidence_document_id,
        source_label=row.source_label,
    )


def ensure_situations(session: Session, pairs: Iterable[tuple[str, int]]) -> list[Situation]:
    """The situations for (item code, place id) pairs, created when missing.

    A pair qualifies when the place is the country or a state and the item has current values for
    two consecutive months there (B8.1). Pairs that do not qualify are left out.
    """
    catalog = load_items()
    found: list[Situation] = []
    for item_code, place_id in sorted(set(pairs)):
        place = session.get(Place, place_id)
        if place is None or place.kind not in SCOPE_KINDS or place.code is None:
            continue
        item = next((i for i in catalog.items if i.code == item_code), None)
        if item is None:
            continue
        months = {r.measurement.period_start for r in _current_rows(session, item_code, [place_id])}
        if not any(_add_months(m, -1) in months for m in months):
            continue
        slug = slug_for(item_code, place.code)
        situation = session.scalars(select(Situation).where(Situation.slug == slug)).one_or_none()
        if situation is None:
            situation = Situation(
                slug=slug,
                kind="price_series",
                topic=item.topic,
                title=_title(item, place),
                item_code=item_code,
                place_id=place_id,
            )
            session.add(situation)
            session.flush()
        found.append(situation)
    return found


def _title(item: Item, place: Place) -> str:
    label = item.label[0].upper() + item.label[1:]
    return f"{label} price in {place_phrase(place.kind, place.code or '', place.name)}"


def load_inputs(session: Session, situation: Situation, now: datetime) -> PriceInputs | None:
    """Everything the T1 computation needs, or None when the place has no current values."""
    if situation.item_code is None:
        return None
    item = load_items().item(situation.item_code)
    place = session.get(Place, situation.place_id)
    if place is None or place.code is None or place.kind not in SCOPE_KINDS:
        return None
    rows = _current_rows(session, item.code, [place.id])
    if not rows:
        return None
    by_month = {r.measurement.period_start: r for r in rows}
    latest = max(by_month)
    current = by_month[latest]
    previous = by_month.get(_add_months(latest, -1))
    year_ago = by_month.get(_add_months(latest, -12))

    states_now: tuple[StatePoint, ...] = ()
    states_before: tuple[StatePoint, ...] = ()
    if place.kind == "country":
        state_places = {
            p.id: p for p in session.scalars(select(Place).where(Place.kind == "state"))
        }
        state_rows = _current_rows(
            session, item.code, state_places, [latest, _add_months(latest, -1)]
        )

        def points(month: date) -> tuple[StatePoint, ...]:
            return tuple(
                StatePoint(
                    state_places[r.measurement.place_id].code or "",
                    state_places[r.measurement.place_id].name,
                    _point(r),
                )
                for r in sorted(state_rows, key=lambda r: r.measurement.place_id)
                if r.measurement.period_start == month
            )

        states_now, states_before = points(latest), points(_add_months(latest, -1))

    return PriceInputs(
        item_code=item.code,
        item_label=item.label,
        unit=item.unit,
        mom_threshold_pct=Decimal(str(item.materiality.mom_pct)),
        yoy_threshold_pct=Decimal(str(item.materiality.yoy_pct)),
        factors=tuple(FactorSpec(f.code, f.label) for f in item.factors),
        place_code=place.code,
        place_name=place.name,
        place_kind="country" if place.kind == "country" else "state",
        source_short=source_short_name(current.source),
        current=_point(current),
        previous=_point(previous) if previous else None,
        year_ago=_point(year_ago) if year_ago else None,
        now=now,
        states_current=states_now,
        states_previous=states_before,
    )


def _change_summary(previous: AssessmentVersion | None, new: PriceAssessment) -> str | None:
    if previous is None:
        return None
    if previous.period_label != new.period_label:
        return f"Updated with {new.period_label} data (previously {previous.period_label})"
    return "Re-assessed after the inputs changed"


def assess_situation(
    session: Session, situation_id: int, now: datetime, *, correction: str | None = None
) -> AssessmentOutcome:
    """Compute the situation's assessment and store a new version if the inputs changed.

    The same inputs give the same ``inputs_hash``, and then nothing new is stored: only the
    latest version's ``last_checked_at`` moves. ``correction`` is the sentence that explains why
    the inputs changed (spec B9): it becomes the new version's ``change_summary``. The caller
    commits.
    """
    situation = session.get(Situation, situation_id)
    if situation is None:
        return AssessmentOutcome("skipped", reason="situation does not exist")
    inputs = load_inputs(session, situation, now)
    if inputs is None:
        return AssessmentOutcome("skipped", reason="no current measurements")

    computed = compute_price_change(inputs)
    latest = session.scalars(
        select(AssessmentVersion)
        .where(AssessmentVersion.situation_id == situation.id)
        .order_by(AssessmentVersion.version.desc())
        .limit(1)
    ).first()
    if latest is not None and latest.inputs_hash == computed.inputs_hash:
        latest.last_checked_at = now
        return AssessmentOutcome("unchanged", latest)

    version = AssessmentVersion(
        situation_id=situation.id,
        version=1 if latest is None else latest.version + 1,
        template="T1_price_change",
        template_version=TEMPLATE_VERSION,
        policy_version=POLICY_UNAPPLIED,
        inputs_hash=computed.inputs_hash,
        status="draft",
        evidence_state=computed.evidence_state,
        severity=computed.severity,
        headline=computed.headline,
        facts=computed.facts,
        possible_factors=computed.possible_factors,
        unknowns=computed.unknowns,
        scope_label=computed.scope_label,
        period_label=computed.period_label,
        last_checked_at=now,
        valid_until=computed.valid_until,
        change_summary=correction or _change_summary(latest, computed),
        withheld_reasons=[],
        supersedes_id=latest.id if latest else None,
    )
    session.add(version)
    session.flush()
    session.add_all(
        AssessmentInput(assessment_version_id=version.id, input_kind=kind, input_id=id_)
        for kind, id_ in computed.inputs
    )
    session.flush()
    log.info(
        "assessment %s v%d: %s",
        situation.slug,
        version.version,
        computed.headline,
        extra={"evidence_state": computed.evidence_state, "severity": computed.severity},
    )
    return AssessmentOutcome("created", version)


def request_assessments(
    session: Session,
    touched: Iterable[tuple[str, int]],
    document_id: int,
    superseded: Iterable[int] = (),
) -> list[int]:
    """Create missing situations for new or changed values and queue an ``assess_situation`` job
    for each. ``touched`` holds (item code, place id) pairs. A changed state value also changes
    the national situation's aggregates, so the country pair is added for every state pair.

    ``superseded`` holds ids of measurements that restated values replaced. A situation whose
    current version used one of them gets a correction (spec B9) instead of a plain
    re-assessment.

    Jobs are deduplicated per situation and source document, so importing the same document twice
    queues nothing new.
    """
    from africasignal.publish.invalidation import plan_corrections, queue_correction

    pairs = set(touched)
    country = session.scalars(select(Place.id).where(Place.code == "NG")).one_or_none()
    if country is not None:
        pairs |= {(item, country) for item, _ in pairs}
    superseded = set(superseded)
    corrections = plan_corrections(session, "measurement", superseded) if superseded else {}
    job_ids: list[int] = []
    for situation in ensure_situations(session, pairs):
        key = f"assess_situation:{situation.id}:{document_id}"
        if situation.id in corrections:
            job_id = queue_correction(session, situation.id, corrections[situation.id], key)
        else:
            job_id = queue.enqueue(
                session, "assess_situation", {"situation_id": situation.id}, dedupe_key=key
            )
        if job_id is not None:
            job_ids.append(job_id)
    return job_ids
