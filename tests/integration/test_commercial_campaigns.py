from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import AuditLog, Campaign, CreativeVersion
from africasignal.operations.commercial_campaigns import (
    CampaignDraft,
    CommercialLifecycleError,
    CreativeDraft,
    create_campaign,
    create_creative_version,
    review_creative,
    transition_campaign,
    transition_sponsor,
    update_campaign_terms,
)
from africasignal.operations.commercial_sponsors import SponsorDraft, create_sponsor
from africasignal.operators import create_operator


def _admin(session: Session):
    operator, _ = create_operator(
        session, "campaign-admin@example.org", "correct horse battery staple", "admin"
    )
    return operator


def _sponsor(session: Session, operator):
    return create_sponsor(
        session,
        operator,
        SponsorDraft(
            public_name="Reviewed sponsor",
            website_url="https://example.org",
            contact_email="private@example.org",
        ),
    )


def _creative() -> CreativeDraft:
    return CreativeDraft(
        body_text="Reviewed sponsorship copy", destination_url="https://example.org/offer"
    )


def test_sponsor_transitions_require_revisions_and_retired_is_terminal(session: Session) -> None:
    operator = _admin(session)
    sponsor = _sponsor(session, operator)

    approved = transition_sponsor(
        session, operator, sponsor.id, status="approved", expected_revision=sponsor.revision
    )
    assert approved.status == "approved" and approved.revision == 2
    with pytest.raises(CommercialLifecycleError, match="revision conflict"):
        transition_sponsor(session, operator, sponsor.id, status="paused", expected_revision=1)
    retired = transition_sponsor(
        session, operator, sponsor.id, status="retired", expected_revision=approved.revision
    )
    with pytest.raises(CommercialLifecycleError, match="invalid sponsor transition"):
        transition_sponsor(
            session, operator, sponsor.id, status="approved", expected_revision=retired.revision
        )


def test_campaign_fee_edit_invalidates_approval_and_creative_is_versioned(session: Session) -> None:
    operator = _admin(session)
    sponsor = _sponsor(session, operator)
    transition_sponsor(session, operator, sponsor.id, status="approved", expected_revision=1)
    campaign = create_campaign(
        session,
        operator,
        CampaignDraft(
            sponsor_id=sponsor.id, internal_name="energy pilot", agreed_fee_minor=125_000
        ),
    )

    with pytest.raises(CommercialLifecycleError, match="invalid campaign transition"):
        transition_campaign(
            session, operator, campaign.id, status="active", expected_revision=campaign.revision
        )
    approved = transition_campaign(
        session, operator, campaign.id, status="approved", expected_revision=campaign.revision
    )
    creative = create_creative_version(session, operator, campaign.id, _creative())
    reviewed = review_creative(
        session, operator, creative.id, status="approved", expected_revision=creative.revision
    )
    with pytest.raises(CommercialLifecycleError, match="eligible active booking"):
        transition_campaign(
            session, operator, campaign.id, status="active", expected_revision=approved.revision
        )
    assert reviewed.status == "approved" and reviewed.version == 1
    paused = transition_campaign(
        session, operator, campaign.id, status="paused", expected_revision=approved.revision
    )
    edited = update_campaign_terms(
        session,
        operator,
        campaign.id,
        internal_name="energy pilot",
        agreed_fee_minor=200_000,
        agreement_reference="agreement-ref",
        expected_revision=paused.revision,
    )
    assert edited.status == "draft" and edited.revision == paused.revision + 1
    assert edited.approved_at is None and edited.approved_by_operator_id is None

    second = create_creative_version(session, operator, campaign.id, _creative())
    assert second.version == 2
    old = session.get(CreativeVersion, creative.id)
    assert old is not None and old.body_text == reviewed.body_text
    action = session.scalars(
        select(AuditLog).where(AuditLog.action == "commercial.campaign.create")
    ).one()
    assert action.after is not None and action.after["agreed_fee_minor"] == 125_000


def test_approved_creative_can_only_be_withdrawn_not_edited(session: Session) -> None:
    operator = _admin(session)
    sponsor = _sponsor(session, operator)
    transition_sponsor(session, operator, sponsor.id, status="approved", expected_revision=1)
    campaign = create_campaign(
        session, operator, CampaignDraft(sponsor_id=sponsor.id, internal_name="food pilot")
    )
    campaign = transition_campaign(
        session, operator, campaign.id, status="approved", expected_revision=campaign.revision
    )
    creative = create_creative_version(session, operator, campaign.id, _creative())
    creative = review_creative(
        session, operator, creative.id, status="approved", expected_revision=creative.revision
    )
    withdrawn = review_creative(
        session, operator, creative.id, status="withdrawn", expected_revision=creative.revision
    )
    assert withdrawn.status == "withdrawn"
    with pytest.raises(CommercialLifecycleError, match="invalid creative transition"):
        review_creative(
            session, operator, creative.id, status="approved", expected_revision=withdrawn.revision
        )
    assert session.get(Campaign, campaign.id) is not None
