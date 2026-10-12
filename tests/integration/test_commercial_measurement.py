from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import DeliveryEvent, PlacementBooking
from africasignal.operations.commercial_bookings import transition_booking
from africasignal.operations.commercial_measurement import (
    ClickTokenError,
    issue_click_token,
    validate_click_destination,
)
from tests.integration.test_commercial_bookings import NOW, _approve, _enable, _system


def test_click_destination_rechecks_active_binding_and_never_stores_raw_token(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    operator, _, booking_id = _system(session)
    _approve(session, operator, booking_id)
    active = transition_booking(
        session, operator, booking_id, status="active", expected_revision=2, at=NOW
    )
    booking = session.get(PlacementBooking, booking_id)
    assert booking is not None

    token = issue_click_token(
        booking_id=booking.id,
        creative_version_id=booking.creative_version_id,
        booking_revision=active.revision,
        at=NOW,
    )
    target = validate_click_destination(session, token, at=NOW)

    assert target.destination_url == "https://example.org/offer"
    assert target.booking_id == booking.id
    assert target.deduplication_hash != token
    assert len(target.deduplication_hash) == 64
    assert session.scalar(select(DeliveryEvent.id)) is None

    transition_booking(
        session,
        operator,
        booking_id,
        status="paused",
        expected_revision=active.revision,
        at=NOW,
    )
    with pytest.raises(ClickTokenError):
        validate_click_destination(session, token, at=NOW + timedelta(seconds=1))


def test_click_token_from_other_booking_cannot_redirect(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    operator1, _, booking1 = _system(session)
    operator2, _, booking2 = _system(session)
    _approve(session, operator1, booking1)
    active = transition_booking(
        session, operator1, booking1, status="active", expected_revision=2, at=NOW
    )
    token = issue_click_token(
        booking_id=booking2, creative_version_id=1, booking_revision=3, at=NOW
    )
    with pytest.raises(ClickTokenError):
        validate_click_destination(session, token, at=NOW)
    assert active.status == "active"
