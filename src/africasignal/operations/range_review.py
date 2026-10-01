"""The measurement range-check queue (spec B6.3, rule R4): values NBS published that fall outside
0.2x to 5x of the previous month's median were not stored. An operator approves one (it is stored
as a measurement and the situation is assessed again) or rejects it. Until a row is decided, R4
withholds assessments that would use that period."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.models import (
    EvidenceDocument,
    MeasurementReview,
    Operator,
    Place,
    Series,
)
from africasignal.operations.assessments import clean_reason
from africasignal.publish.situations import request_assessments
from africasignal.sources.nbs import store_measurement


class ReviewError(ValueError):
    """A refusal the operator should see."""


@dataclass(frozen=True)
class ReviewRow:
    review: MeasurementReview
    series: Series
    place: Place

    @property
    def ratio(self) -> str:
        if not self.review.reference_value:
            return "n/a"
        return f"{self.review.value / self.review.reference_value:.2f}x"


def pending(session: Session, limit: int = 200) -> list[ReviewRow]:
    rows = session.execute(
        select(MeasurementReview, Series, Place)
        .join(Series, Series.id == MeasurementReview.series_id)
        .join(Place, Place.id == MeasurementReview.place_id)
        .where(MeasurementReview.status == "pending")
        .order_by(MeasurementReview.id)
        .limit(limit)
    )
    return [ReviewRow(r, s, p) for r, s, p in rows]


def recently_decided(session: Session, limit: int = 20) -> list[ReviewRow]:
    rows = session.execute(
        select(MeasurementReview, Series, Place)
        .join(Series, Series.id == MeasurementReview.series_id)
        .join(Place, Place.id == MeasurementReview.place_id)
        .where(MeasurementReview.status != "pending")
        .order_by(MeasurementReview.resolved_at.desc().nullslast(), MeasurementReview.id.desc())
        .limit(limit)
    )
    return [ReviewRow(r, s, p) for r, s, p in rows]


def _pending_row(session: Session, review_id: int) -> MeasurementReview:
    review = session.scalars(
        select(MeasurementReview).where(MeasurementReview.id == review_id).with_for_update()
    ).first()
    if review is None:
        raise ReviewError("no such value in the queue")
    if review.status != "pending":
        raise ReviewError(f"that value was already {review.status}")
    return review


def approve(
    session: Session,
    operator: Operator,
    review_id: int,
    note: str,
    now: datetime | None = None,
) -> MeasurementReview:
    """Store the value as a measurement and queue the assessments that use it."""
    now = now or datetime.now(UTC)
    note = clean_reason(note)
    review = _pending_row(session, review_id)
    series = session.get(Series, review.series_id)
    document = session.get(EvidenceDocument, review.evidence_document_id)
    assert series is not None and document is not None
    superseded: list[int] = []
    outcome = store_measurement(
        session,
        series,
        review.place_id,
        review.period_start,
        review.value,
        review.vintage,
        document,
        superseded,
    )
    if outcome == "conflict":
        raise ReviewError(
            "this release already holds a different value for the same place and month; "
            "reject this one instead"
        )
    review.status = "approved"
    review.resolved_at = now
    session.flush()
    queued = request_assessments(
        session,
        {(series.item_code, review.place_id)},
        document.id,
        superseded,
        dedupe_tag=f"review:{review.id}",
    )
    audit.record(
        session,
        operator,
        "measurement_review.approve",
        "measurement_review",
        review.id,
        before={"status": "pending", "value": str(review.value)},
        after={
            "status": "approved",
            "item_code": series.item_code,
            "period_start": review.period_start.isoformat(),
            "stored_as": outcome,
            "assessments_queued": len(queued),
            "note": note,
        },
    )
    return review


def reject(
    session: Session,
    operator: Operator,
    review_id: int,
    note: str,
    now: datetime | None = None,
) -> MeasurementReview:
    """Discard the value. The assessments it was holding back (R4) are run again without it."""
    now = now or datetime.now(UTC)
    note = clean_reason(note)
    review = _pending_row(session, review_id)
    series = session.get(Series, review.series_id)
    assert series is not None
    review.status = "rejected"
    review.resolved_at = now
    session.flush()
    queued = request_assessments(
        session,
        {(series.item_code, review.place_id)},
        review.evidence_document_id,
        dedupe_tag=f"review:{review.id}",
    )
    audit.record(
        session,
        operator,
        "measurement_review.reject",
        "measurement_review",
        review.id,
        before={"status": "pending", "value": str(review.value)},
        after={
            "status": "rejected",
            "item_code": series.item_code,
            "period_start": review.period_start.isoformat(),
            "assessments_queued": len(queued),
            "note": note,
        },
    )
    return review
