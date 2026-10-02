from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import DeliveryAggregate, DeliveryEvent, PlacementBooking
from africasignal.operations.commercial_bookings import transition_booking
from africasignal.operations.commercial_reporting import (
    build_report,
    prune_delivery_data,
    rebuild_recent_aggregates,
)
from tests.integration.test_commercial_bookings import NOW, _approve, _enable, _system


def _active(session: Session, monkeypatch: pytest.MonkeyPatch) -> int:
    _enable(monkeypatch)
    operator, _, booking_id = _system(session)
    _approve(session, operator, booking_id)
    transition_booking(session, operator, booking_id, status="active", expected_revision=2, at=NOW)
    booking = session.get(PlacementBooking, booking_id)
    assert booking is not None
    return booking.id


def test_daily_aggregates_rebuild_from_retained_raw_events(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booking_id = _active(session, monkeypatch)
    booking = session.get(PlacementBooking, booking_id)
    assert booking is not None
    moment = NOW - timedelta(days=2, hours=3)
    for metric, count in (("server_render", 3), ("click", 2)):
        for index in range(count):
            token_digest = hashlib.sha256(f"{metric}-{index}".encode()).hexdigest()
            session.add(
                DeliveryEvent(
                    booking_id=booking.id,
                    creative_version_id=booking.creative_version_id,
                    booking_revision=booking.revision,
                    event_schema_version=1,
                    metric=metric,
                    metric_version=1,
                    deduplication_hash=token_digest,
                    received_at=moment,
                    validity_status="accepted",
                    rejection_reason=None,
                    retention_until=NOW + timedelta(days=27),
                )
            )
    session.flush()

    first_rebuild = rebuild_recent_aggregates(session, days=7, now=NOW)
    first = build_report(session, days=7, now=NOW)
    second_rebuild = rebuild_recent_aggregates(session, days=7, now=NOW)
    second = build_report(session, days=7, now=NOW)

    assert first_rebuild == second_rebuild == 2
    assert [(line.metric, line.count) for line in first.lines] == [
        (line.metric, line.count) for line in second.lines
    ]
    assert all(line.completeness == "partial" for line in second.lines)
    metrics = {metric.metric: metric for metric in second.metrics}
    assert metrics["server_render"].count == 3
    assert metrics["click"].count == 2
    assert metrics["eligible_opportunity"].count is None
    assert metrics["eligible_opportunity"].availability == "unavailable"
    assert second.unavailable_metrics == ("client_viewability", "provider_billed")
    assert not any(metric in {"client_viewability", "provider_billed"} for metric in metrics)


def test_empty_period_is_unavailable_not_zero_and_retention_is_enforced(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booking_id = _active(session, monkeypatch)
    booking = session.get(PlacementBooking, booking_id)
    assert booking is not None
    expired_at = NOW - timedelta(days=2)
    session.add(
        DeliveryEvent(
            booking_id=booking.id,
            creative_version_id=booking.creative_version_id,
            booking_revision=booking.revision,
            event_schema_version=1,
            metric="click",
            metric_version=1,
            deduplication_hash=hashlib.sha256(b"expired-event").hexdigest(),
            received_at=expired_at - timedelta(days=30),
            validity_status="accepted",
            rejection_reason=None,
            retention_until=expired_at,
        )
    )
    session.add(
        DeliveryAggregate(
            booking_id=booking.id,
            creative_version_id=booking.creative_version_id,
            surface=booking.surface,
            topic=booking.topic,
            metric="click",
            metric_version=1,
            period_start=expired_at - timedelta(days=1),
            count=4,
            completeness="partial",
            retention_until=expired_at,
        )
    )
    session.flush()

    empty_report = build_report(session, days=7, now=NOW)
    deleted_raw, deleted_aggregates = prune_delivery_data(session, now=NOW)

    assert not empty_report.lines
    assert all(metric.count is None for metric in empty_report.metrics)
    assert all(metric.availability == "unavailable" for metric in empty_report.metrics)
    assert (deleted_raw, deleted_aggregates) == (1, 1)
    assert session.scalars(select(DeliveryEvent)).all() == []
    assert session.scalars(select(DeliveryAggregate)).all() == []
