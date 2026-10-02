"""Rebuildable, private first-party delivery reports with conservative completeness."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from africasignal.models import DeliveryAggregate, DeliveryEvent, PlacementBooking

REPORT_VERSION = 1
METRIC_VERSION = 1
MAX_REBUILD_DAYS = 30
AGGREGATE_RETENTION_MONTHS = 24


class ReportMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: str
    label: str
    count: int | None
    availability: Literal["partial", "unavailable"]
    denominator: str
    attribution: str
    source: str = "AfricaSignal first-party server observations"


class ReportLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    booking_id: int
    creative_version_id: int
    surface: str
    topic: str
    metric: Literal["eligible_opportunity", "server_render", "click"]
    metric_version: int
    period_start: datetime
    count: int
    completeness: Literal["complete", "partial", "unavailable"]


class CommercialReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_version: Literal[1]
    generated_at: datetime
    period_start: datetime
    period_end: datetime
    raw_retention_days: Literal[30]
    aggregate_retention_months: Literal[24]
    lines: tuple[ReportLine, ...]
    metrics: tuple[ReportMetric, ...]
    unavailable_metrics: tuple[str, ...] = ("client_viewability", "provider_billed")


class ReportError(ValueError):
    """Bounded error for private report and aggregation requests."""


def _aware_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ReportError("report times must include a timezone")
    return moment.astimezone(UTC)


def _add_months(moment: datetime, months: int) -> datetime:
    index = moment.year * 12 + moment.month - 1 + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def rebuild_recent_aggregates(
    session: Session,
    *,
    days: int,
    now: datetime,
) -> int:
    """Replace aggregate buckets from raw rows still inside their retention window.

    Caller owns commit/rollback. Empty days deliberately produce no zero rows because no
    independent coverage ledger yet proves that collection ran continuously for those days.
    """
    moment = _aware_utc(now)
    if not 1 <= days <= MAX_REBUILD_DAYS:
        raise ReportError(f"days must be between 1 and {MAX_REBUILD_DAYS}")
    period_start = moment - timedelta(days=days)
    retention_floor = moment - timedelta(days=30)
    if period_start < retention_floor:
        raise ReportError("requested range extends beyond raw event retention")
    period_end = moment
    utc_day_start = period_start.replace(hour=0, minute=0, second=0, microsecond=0)

    session.execute(
        delete(DeliveryAggregate).where(
            DeliveryAggregate.period_start >= utc_day_start,
            DeliveryAggregate.period_start < period_end,
        )
    )
    bucket = func.date_trunc("day", DeliveryEvent.received_at, "UTC").label("period_start")
    grouped = session.execute(
        select(
            DeliveryEvent.booking_id,
            DeliveryEvent.creative_version_id,
            PlacementBooking.surface,
            PlacementBooking.topic,
            DeliveryEvent.metric,
            DeliveryEvent.metric_version,
            bucket,
            func.count(DeliveryEvent.id),
        )
        .join(PlacementBooking, PlacementBooking.id == DeliveryEvent.booking_id)
        .where(
            DeliveryEvent.validity_status == "accepted",
            DeliveryEvent.received_at >= period_start,
            DeliveryEvent.received_at < period_end,
        )
        .group_by(
            DeliveryEvent.booking_id,
            DeliveryEvent.creative_version_id,
            PlacementBooking.surface,
            PlacementBooking.topic,
            DeliveryEvent.metric,
            DeliveryEvent.metric_version,
            bucket,
        )
        .order_by(bucket, DeliveryEvent.booking_id, DeliveryEvent.metric)
    ).all()
    if not grouped:
        return 0

    values = [
        {
            "booking_id": booking_id,
            "creative_version_id": creative_id,
            "surface": surface,
            "topic": topic,
            "metric": metric,
            "metric_version": metric_version,
            "period_start": bucket_start,
            "count": int(count),
            "completeness": "partial",
            "retention_until": _add_months(bucket_start, AGGREGATE_RETENTION_MONTHS),
            "rebuilt_at": moment,
        }
        for (
            booking_id,
            creative_id,
            surface,
            topic,
            metric,
            metric_version,
            bucket_start,
            count,
        ) in grouped
    ]
    statement = pg_insert(DeliveryAggregate).values(values)
    statement = statement.on_conflict_do_update(
        index_elements=[
            DeliveryAggregate.booking_id,
            DeliveryAggregate.creative_version_id,
            DeliveryAggregate.metric,
            DeliveryAggregate.metric_version,
            DeliveryAggregate.period_start,
        ],
        set_={
            "count": statement.excluded.count,
            "surface": statement.excluded.surface,
            "topic": statement.excluded.topic,
            "completeness": statement.excluded.completeness,
            "retention_until": statement.excluded.retention_until,
            "rebuilt_at": statement.excluded.rebuilt_at,
        },
    )
    session.execute(statement)
    return len(values)


def build_report(session: Session, *, days: int, now: datetime) -> CommercialReport:
    moment = _aware_utc(now)
    if not 1 <= days <= MAX_REBUILD_DAYS:
        raise ReportError(f"days must be between 1 and {MAX_REBUILD_DAYS}")
    start = moment - timedelta(days=days)
    rows = tuple(
        ReportLine(
            booking_id=row.booking_id,
            creative_version_id=row.creative_version_id,
            surface=row.surface,
            topic=row.topic,
            metric=row.metric,  # type: ignore[arg-type]
            metric_version=row.metric_version,
            period_start=row.period_start,
            count=row.count,
            completeness=row.completeness,  # type: ignore[arg-type]
        )
        for row in session.scalars(
            select(DeliveryAggregate)
            .where(
                DeliveryAggregate.period_start
                >= start.replace(hour=0, minute=0, second=0, microsecond=0),
                DeliveryAggregate.period_start < moment,
                DeliveryAggregate.retention_until > moment,
            )
            .order_by(
                DeliveryAggregate.period_start,
                DeliveryAggregate.booking_id,
                DeliveryAggregate.metric,
            )
        )
    )
    definitions: tuple[tuple[str, str, str, str], ...] = (
        (
            "eligible_opportunity",
            "Eligible opportunities",
            "No denominator is claimed; this is a server-side eligibility check "
            + "attached to an Explore response.",
            "One operational count per eligible response, deduplicated by server-generated "
            + "event key.",
        ),
        (
            "server_render",
            "Server renders",
            "Eligible opportunities are the operational denominator; these are not human views.",
            "One server-side rendering observation; bots may be included and no human/viewability "
            + "claim is made.",
        ),
        (
            "click",
            "Validated clicks",
            "Server renders are the operational denominator; click-through is not causal lift.",
            "One validated first-party redirect token, counted once by its one-way token hash.",
        ),
    )
    metrics = tuple(
        ReportMetric(
            metric=metric,
            label=label,
            count=sum(row.count for row in rows if row.metric == metric)
            if any(row.metric == metric for row in rows)
            else None,
            availability="partial" if any(row.metric == metric for row in rows) else "unavailable",
            denominator=denominator,
            attribution=attribution,
        )
        for metric, label, denominator, attribution in definitions
    )
    return CommercialReport(
        report_version=1,
        generated_at=moment,
        period_start=start,
        period_end=moment,
        raw_retention_days=30,
        aggregate_retention_months=24,
        lines=rows,
        metrics=metrics,
    )


def prune_delivery_data(session: Session, *, now: datetime) -> tuple[int, int]:
    """Delete raw observations and aggregate buckets at their declared retention deadlines."""
    moment = _aware_utc(now)
    raw_result = cast(
        CursorResult[Any],
        session.execute(delete(DeliveryEvent).where(DeliveryEvent.retention_until <= moment)),
    )
    aggregate_result = cast(
        CursorResult[Any],
        session.execute(
            delete(DeliveryAggregate).where(DeliveryAggregate.retention_until <= moment)
        ),
    )
    raw = raw_result.rowcount
    aggregates = aggregate_result.rowcount
    return int(raw or 0), int(aggregates or 0)
