from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import operators
from africasignal.models import (
    AuditLog,
    Campaign,
    CreativeVersion,
    Operator,
    PlacementBooking,
    Sponsor,
)
from africasignal.web import deps
from africasignal.web.app import create_app
from africasignal.web.session_dep import get_db as public_get_db

PASSWORD = "correct horse battery staple"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app = create_app()

    def override_db():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    app.dependency_overrides[deps.get_db] = override_db
    app.dependency_overrides[public_get_db] = override_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def _sign_in(client: TestClient, session: Session) -> Operator:
    operator, secret = operators.create_operator(
        session,
        f"commercial-admin-{uuid4().hex[:10]}@example.org",
        PASSWORD,
        "admin",
    )
    response = client.post(
        "/admin/login",
        data={
            "email": operator.email,
            "password": PASSWORD,
            "code": pyotp.TOTP(secret).now(),
        },
        headers=ORIGIN,
    )
    assert response.status_code == 303
    return operator


def test_commercial_console_is_admin_only_csrf_guarded_and_revisioned(
    session: Session,
    client: TestClient,
) -> None:
    denied = client.get("/admin/commercial")
    assert denied.status_code == 303
    assert denied.headers["location"] == "/admin/login"

    _sign_in(client, session)
    report_page = client.get("/admin/commercial/reports")
    assert report_page.status_code == 200
    assert "Provider-billed quantity" in report_page.text
    assert "not a zero-event claim" in report_page.text

    dashboard = client.get("/admin/commercial")
    assert dashboard.status_code == 200
    assert "Blocked by the deployment-owned COMMERCIAL_DENY flag." in dashboard.text
    assert (
        "Commercial sponsorship privacy and measurement review is not confirmed." in dashboard.text
    )
    assert "Deterministic context refresh" in dashboard.text
    assert "Contextual matches and package drafts" in dashboard.text

    denied_context_rebuild = client.post("/admin/commercial/context/rebuild")
    assert denied_context_rebuild.status_code == 403
    blocked_context_rebuild = client.post(
        "/admin/commercial/context/rebuild",
        headers=ORIGIN,
    )
    assert blocked_context_rebuild.status_code == 409
    assert "deployment deny" in blocked_context_rebuild.text

    no_origin = client.post(
        "/admin/commercial/sponsors",
        data={
            "public_name": "Denied Sponsor",
            "website_url": "https://denied.example.org",
            "contact_email": "private@denied.example.org",
        },
    )
    assert no_origin.status_code == 403
    assert session.scalars(select(Sponsor)).all() == []

    extra_field = client.post(
        "/admin/commercial/sponsors",
        data={
            "public_name": "Denied Sponsor",
            "website_url": "https://denied.example.org",
            "contact_email": "private@denied.example.org",
            "unexpected": "rejected",
        },
        headers=ORIGIN,
    )
    assert extra_field.status_code == 400
    assert session.scalars(select(Sponsor)).all() == []

    created = client.post(
        "/admin/commercial/sponsors",
        data={
            "public_name": "Reviewed Example Sponsor",
            "website_url": "https://sponsor.example.org",
            "contact_email": "private.owner@example.org",
        },
        headers=ORIGIN,
    )
    assert created.status_code == 303
    sponsor = session.scalar(select(Sponsor))
    assert sponsor is not None and sponsor.status == "pending_review"
    sponsor_audit = session.scalar(
        select(AuditLog).where(AuditLog.action == "commercial.sponsor.create")
    )
    assert sponsor_audit is not None
    assert "private.owner@example.org" not in str(sponsor_audit.after)

    approved_sponsor = client.post(
        f"/admin/commercial/sponsors/{sponsor.id}/status",
        data={"status": "approved", "expected_revision": "1"},
        headers=ORIGIN,
    )
    assert approved_sponsor.status_code == 303
    assert sponsor.status == "approved"

    created_campaign = client.post(
        "/admin/commercial/campaigns",
        data={
            "sponsor_id": str(sponsor.id),
            "internal_name": "manual-flat-fee",
            "agreed_fee_minor": "12500",
            "agreement_reference": "agreement-record-1",
        },
        headers=ORIGIN,
    )
    assert created_campaign.status_code == 303
    campaign = session.scalar(select(Campaign))
    assert campaign is not None
    assert campaign.agreed_fee_minor == 12_500
    assert campaign.currency == "NGN"

    approved_campaign = client.post(
        f"/admin/commercial/campaigns/{campaign.id}/status",
        data={"status": "approved", "expected_revision": str(campaign.revision)},
        headers=ORIGIN,
    )
    assert approved_campaign.status_code == 303
    assert campaign.status == "approved"

    created_creative = client.post(
        f"/admin/commercial/campaigns/{campaign.id}/creatives",
        data={
            "body_text": "A reviewed plain-text sponsor message.",
            "destination_url": "https://sponsor.example.org/offer",
        },
        headers=ORIGIN,
    )
    assert created_creative.status_code == 303
    creative = session.scalar(select(CreativeVersion))
    assert creative is not None and creative.status == "draft"
    reviewed_creative = client.post(
        f"/admin/commercial/creatives/{creative.id}/review",
        data={"status": "approved", "expected_revision": str(creative.revision)},
        headers=ORIGIN,
    )
    assert reviewed_creative.status_code == 303
    assert creative.status == "approved"

    now = datetime.now(UTC)
    created_booking = client.post(
        "/admin/commercial/bookings",
        data={
            "campaign_id": str(campaign.id),
            "topic": "energy",
            "starts_at": (now - timedelta(minutes=1)).isoformat(),
            "ends_at": (now + timedelta(days=1)).isoformat(),
            "creative_version_id": str(creative.id),
        },
        headers=ORIGIN,
    )
    assert created_booking.status_code == 303
    booking = session.scalar(select(PlacementBooking))
    assert booking is not None and booking.status == "draft"

    stale_revision = client.post(
        f"/admin/commercial/bookings/{booking.id}/status",
        data={"status": "ended", "expected_revision": "99"},
        headers=ORIGIN,
    )
    assert stale_revision.status_code == 409
    assert booking.status == "draft"
    assert "private.owner@example.org" in client.get("/admin/commercial").text


def test_approved_campaign_cannot_activate_without_an_active_booking(
    session: Session,
    client: TestClient,
) -> None:
    operator = _sign_in(client, session)
    sponsor = Sponsor(
        public_name="Reviewed Example Sponsor",
        website_url="https://sponsor.example.org",
        contact_email="private.owner@example.org",
        status="approved",
        revision=1,
        created_by_operator_id=operator.id,
        updated_by_operator_id=operator.id,
    )
    session.add(sponsor)
    session.flush()
    campaign = Campaign(
        sponsor_id=sponsor.id,
        internal_name=f"campaign-{uuid4().hex[:8]}",
        status="approved",
        revision=2,
        currency="NGN",
        created_by_operator_id=operator.id,
    )
    session.add(campaign)
    session.flush()
    creative = CreativeVersion(
        campaign_id=campaign.id,
        version=1,
        revision=2,
        body_text="approved creative",
        destination_url="https://sponsor.example.org/offer",
        status="approved",
        created_by_operator_id=operator.id,
    )
    session.add(creative)
    session.flush()

    response = client.post(
        f"/admin/commercial/campaigns/{campaign.id}/status",
        data={"status": "active", "expected_revision": "2"},
        headers=ORIGIN,
    )

    assert response.status_code == 400
    assert "eligible active booking" in response.text
    assert campaign.status == "approved"


def test_report_rebuild_is_admin_post_and_limited_to_retained_range(
    session: Session,
    client: TestClient,
) -> None:
    _sign_in(client, session)
    rejected = client.post(
        "/admin/commercial/reports/rebuild",
        data={"days": "31"},
        headers=ORIGIN,
    )
    assert rejected.status_code == 400
    assert "limited to 1-30 days" in rejected.text
    accepted = client.post(
        "/admin/commercial/reports/rebuild",
        data={"days": "7"},
        headers=ORIGIN,
    )
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/admin/commercial/reports?days=7"
