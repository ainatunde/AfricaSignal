from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from africasignal.models import Operator
from africasignal.operations import commercial_bookings as bookings
from africasignal.operations.commercial_bookings import (
    BookingDraft,
    BookingError,
    create_booking,
    eligible_placement,
    transition_booking,
)
from africasignal.operations.commercial_campaigns import (
    CampaignDraft,
    CommercialLifecycleError,
    CreativeDraft,
    create_campaign,
    create_creative_version,
    review_creative,
    transition_campaign,
    transition_sponsor,
)
from africasignal.operations.commercial_sponsors import SponsorDraft, create_sponsor
from africasignal.operators import create_operator

NOW = datetime.now(UTC)


def _enable(monkeypatch: pytest.MonkeyPatch, *, context: bool = True) -> None:
    monkeypatch.setattr(
        bookings, "commercial_state", lambda *_a, **_kw: {"explore_effective": True}
    )
    monkeypatch.setattr(bookings, "_has_eligible_context", lambda *_a, **_kw: context)
    monkeypatch.setattr(bookings, "publication_suspended", lambda *_a, **_kw: False)


def _system(
    session: Session,
    *,
    now: datetime = NOW,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    topic: str = "energy",
) -> tuple[Operator, int, int]:
    suffix = uuid4().hex[:10]
    operator, _ = create_operator(
        session, f"booking-admin-{suffix}@example.org", "correct horse battery staple", "admin"
    )
    sponsor = create_sponsor(
        session,
        operator,
        SponsorDraft(
            public_name=f"Reviewed sponsor {suffix}",
            website_url="https://example.org",
            contact_email=f"private-{suffix}@example.org",
        ),
    )
    transition_sponsor(session, operator, sponsor.id, status="approved", expected_revision=1)
    campaign = create_campaign(
        session,
        operator,
        CampaignDraft(
            sponsor_id=sponsor.id, internal_name=f"pilot-{suffix}", agreed_fee_minor=250_000
        ),
    )
    transition_campaign(
        session, operator, campaign.id, status="approved", expected_revision=campaign.revision
    )
    creative = create_creative_version(
        session,
        operator,
        campaign.id,
        CreativeDraft(
            body_text="Reviewed sponsor copy", destination_url="https://example.org/offer"
        ),
    )
    approved_creative = review_creative(
        session, operator, creative.id, status="approved", expected_revision=creative.revision
    )
    booking = create_booking(
        session,
        operator,
        campaign.id,
        BookingDraft(
            topic=topic,
            starts_at=starts_at or now - timedelta(minutes=5),
            ends_at=ends_at or now + timedelta(hours=1),
            creative_version_id=approved_creative.id,
        ),
    )
    return operator, campaign.id, booking.id


def _approve(session: Session, operator: Operator, booking_id: int):
    return transition_booking(
        session, operator, booking_id, status="approved", expected_revision=1, at=NOW
    )


def test_booking_requires_effective_switch_and_current_eligible_context(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator, _, booking_id = _system(session)
    _enable(monkeypatch, context=False)

    with pytest.raises(BookingError, match="context"):
        transition_booking(
            session, operator, booking_id, status="approved", expected_revision=1, at=NOW
        )
    assert session.get(bookings.PlacementBooking, booking_id).status == "draft"

    monkeypatch.setattr(
        bookings, "commercial_state", lambda *_a, **_kw: {"explore_effective": False}
    )
    _enable(monkeypatch, context=True)
    monkeypatch.setattr(
        bookings, "commercial_state", lambda *_a, **_kw: {"explore_effective": False}
    )
    with pytest.raises(BookingError, match="not currently effective"):
        transition_booking(
            session, operator, booking_id, status="approved", expected_revision=1, at=NOW
        )


def test_overlap_is_checked_at_activation_and_half_open_edges_do_not_conflict(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    operator1, campaign1, booking1 = _system(session)
    operator2, campaign2, booking2 = _system(session)
    _approve(session, operator1, booking1)
    _approve(session, operator2, booking2)

    activated = transition_booking(
        session, operator1, booking1, status="active", expected_revision=2, at=NOW
    )
    assert activated.status == "active"
    with pytest.raises(BookingError, match="booking conflict"):
        transition_booking(
            session, operator2, booking2, status="active", expected_revision=2, at=NOW
        )
    with pytest.raises(CommercialLifecycleError, match="active booking"):
        transition_campaign(session, operator2, campaign2, status="active", expected_revision=2)

    boundary = NOW + timedelta(hours=2)
    operator3, _, before = _system(
        session, starts_at=NOW - timedelta(hours=1), ends_at=boundary, topic="food"
    )
    operator4, _, after = _system(
        session, starts_at=boundary, ends_at=boundary + timedelta(hours=1), topic="food"
    )
    _approve(session, operator3, before)
    _approve(session, operator4, after)
    transition_booking(session, operator3, before, status="active", expected_revision=2, at=NOW)
    next_active = transition_booking(
        session, operator4, after, status="active", expected_revision=2, at=boundary
    )
    assert next_active.status == "active"


def test_public_projection_is_redacted_and_pause_suppresses_render(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    operator, campaign_id, booking_id = _system(session)
    _approve(session, operator, booking_id)
    active = transition_booking(
        session, operator, booking_id, status="active", expected_revision=2, at=NOW
    )
    projection = eligible_placement(
        session, surface="explore_topic", topic="energy", item_code="pms_litre", at=NOW
    )
    assert projection.eligible and projection.booking_revision == active.revision
    assert (
        projection.public_name is not None
        and projection.destination_url == "https://example.org/offer"
    )
    assert "contact_email" not in projection.model_dump()
    assert "agreed_fee_minor" not in projection.model_dump()

    paused = transition_booking(
        session, operator, booking_id, status="paused", expected_revision=active.revision, at=NOW
    )
    assert paused.status == "paused"
    assert session.get(bookings.Campaign, campaign_id).status == "paused"
    assert (
        eligible_placement(
            session, surface="explore_topic", topic="energy", item_code=None, at=NOW
        ).reason_code
        == "unfilled"
    )


def test_two_postgres_sessions_cannot_activate_overlapping_bookings(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    with Session(engine) as setup:
        op1, _, booking1 = _system(setup)
        op2, _, booking2 = _system(setup)
        _approve(setup, op1, booking1)
        _approve(setup, op2, booking2)
        op1_id, op2_id = op1.id, op2.id
        setup.commit()

    def activate(operator_id: int, booking_id: int) -> str:
        with Session(engine) as worker:
            operator = worker.get(Operator, operator_id)
            assert operator is not None
            try:
                transition_booking(
                    worker, operator, booking_id, status="active", expected_revision=2, at=NOW
                )
                worker.commit()
                return "active"
            except BookingError as exc:
                worker.rollback()
                return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: activate(*args),
                [(op1_id, booking1), (op2_id, booking2)],
            )
        )
    assert sorted(result == "active" for result in results) == [False, True]
    assert sum("booking conflict" in result for result in results) == 1
