from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from africasignal.models import DeliveryEvent, PlacementBooking
from africasignal.operations.commercial_bookings import transition_booking
from africasignal.operations.commercial_measurement import (
    DeliveryEnvelope,
    issue_click_token,
    record_delivery_event,
)
from africasignal.web.routes import public
from tests.integration.test_commercial_bookings import NOW, _approve, _enable, _system


def _active_booking(session: Session, monkeypatch: pytest.MonkeyPatch) -> tuple[int, int, int]:
    monkeypatch.setattr(public, "_now", lambda: NOW)
    _enable(monkeypatch)
    operator, _, booking_id = _system(session)
    _approve(session, operator, booking_id)
    active = transition_booking(
        session, operator, booking_id, status="active", expected_revision=2, at=NOW
    )
    booking = session.get(PlacementBooking, booking_id)
    assert booking is not None
    public.commercial_click_limiter.reset()
    return booking.id, booking.creative_version_id, active.revision


def test_click_route_redirects_to_stored_destination_and_replay_counts_once(
    session: Session,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booking_id, creative_id, revision = _active_booking(session, monkeypatch)
    token = issue_click_token(
        booking_id=booking_id,
        creative_version_id=creative_id,
        booking_revision=revision,
        at=NOW,
    )

    first = client.get(f"/commercial/click/{token}", follow_redirects=False)
    second = client.get(f"/commercial/click/{token}", follow_redirects=False)

    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == "https://example.org/offer"
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in first.headers
    events = session.scalars(select(DeliveryEvent)).all()
    assert len(events) == 1
    assert events[0].metric == "click"
    assert events[0].deduplication_hash != token
    assert token not in repr(events[0])


def test_click_route_rejects_redirect_parameters_and_forged_tokens(
    session: Session,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booking_id, creative_id, revision = _active_booking(session, monkeypatch)
    token = issue_click_token(
        booking_id=booking_id,
        creative_version_id=creative_id,
        booking_revision=revision,
        at=NOW,
    )

    supplied_target = client.get(
        f"/commercial/click/{token}?destination=https%3A%2F%2Fevil.example",
        follow_redirects=False,
    )
    forged = client.get(
        f"/commercial/click/{token}x",
        follow_redirects=False,
    )

    assert supplied_target.status_code == 400
    assert forged.status_code == 404
    assert session.scalars(select(DeliveryEvent)).all() == []


def test_click_route_rechecks_booking_pause(
    session: Session,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booking_id, creative_id, revision = _active_booking(session, monkeypatch)
    token = issue_click_token(
        booking_id=booking_id,
        creative_version_id=creative_id,
        booking_revision=revision,
        at=NOW,
    )
    booking = session.get(PlacementBooking, booking_id)
    assert booking is not None
    operator_id = booking.created_by_operator_id
    assert operator_id is not None
    from africasignal.models import Operator

    operator = session.get(Operator, operator_id)
    assert operator is not None
    transition_booking(
        session,
        operator,
        booking_id,
        status="paused",
        expected_revision=revision,
        at=NOW + timedelta(seconds=1),
    )

    response = client.get(f"/commercial/click/{token}", follow_redirects=False)

    assert response.status_code == 404
    assert session.scalars(select(DeliveryEvent)).all() == []


def test_explore_renders_labelled_sponsor_after_content_and_is_not_cacheable(
    session: Session,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _active_booking(session, monkeypatch)

    response = client.get("/explore?topic=energy")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Sponsored" in response.text
    assert "Reviewed sponsor" in response.text
    assert response.text.index("</ul>") < response.text.index('<aside class="sponsor-module"')
    events = session.scalars(select(DeliveryEvent).order_by(DeliveryEvent.metric)).all()
    assert sorted(event.metric for event in events) == ["eligible_opportunity", "server_render"]
    assert re.search(r'href="/commercial/click/[A-Za-z0-9_.-]+"', response.text)


def test_explore_keeps_editorial_page_when_observation_write_fails(
    session: Session,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _active_booking(session, monkeypatch)

    def fail_observation(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("observation store unavailable")

    monkeypatch.setattr(public, "record_placement_observation", fail_observation)
    response = client.get("/explore?topic=energy")

    assert response.status_code == 200
    assert "AfricaSignal" in response.text
    assert "Sponsored" not in response.text


def test_concurrent_click_replay_is_atomically_deduplicated(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(public, "_now", lambda: NOW)
    _enable(monkeypatch)
    from tests.integration.test_commercial_bookings import _system

    with Session(engine) as setup:
        operator, _, booking_id = _system(setup)
        _approve(setup, operator, booking_id)
        active = transition_booking(
            setup, operator, booking_id, status="active", expected_revision=2, at=NOW
        )
        booking = setup.get(PlacementBooking, booking_id)
        assert booking is not None
        token = issue_click_token(
            booking_id=booking_id,
            creative_version_id=booking.creative_version_id,
            booking_revision=active.revision,
            at=NOW,
        )
        setup.commit()

    def record() -> str:
        with Session(engine) as worker:
            receipt = record_delivery_event(
                worker,
                envelope=DeliveryEnvelope(event_schema_version=1, event_kind="click", token=token),
                received_at=NOW,
            )
            worker.commit()
            return receipt.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: record(), range(2)))

    assert sorted(results) == ["accepted", "duplicate"]
    with Session(engine) as verify_session:
        events = verify_session.scalars(select(DeliveryEvent)).all()
        assert len(events) == 1
