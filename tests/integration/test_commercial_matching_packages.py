from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.jobs.handlers import JobContext
from africasignal.jobs.queue import ClaimedJob
from africasignal.models import (
    AssessmentVersion,
    CommercialControl,
    ContentContext,
    EvidenceDocument,
    Place,
    PlacementBooking,
    Situation,
    Source,
    SourcePermission,
    WorkloadControl,
)
from africasignal.operations.commercial_campaigns import (
    CampaignDraft,
    create_campaign,
    transition_campaign,
    transition_sponsor,
)
from africasignal.operations.commercial_context import (
    classify_content,
    content_ref_for,
    store_context_decision,
)
from africasignal.operations.commercial_matching import match_sponsors
from africasignal.operations.commercial_packages import (
    PackageError,
    PackageRequest,
    create_package_draft,
    review_package_draft,
)
from africasignal.operations.commercial_sponsors import SponsorDraft, create_sponsor
from africasignal.operators import create_operator

NOW = datetime.now(UTC)


def _content(session: Session, *, topic: str = "energy", evidence_state: str = "corroborated"):
    suffix = uuid4().hex[:10]
    place = Place(kind="country", name="Nigeria", code=f"NG-PKG-{suffix}")
    session.add(place)
    session.flush()
    situation = Situation(
        slug=f"package-test-{suffix}",
        kind="price_series",
        topic=topic,
        title=f"{topic.title()} update",
        item_code="pms_litre" if topic == "energy" else "rice_kg",
        place_id=place.id,
        status="active",
    )
    source = Source(
        slug=f"package-source-{suffix}",
        name="Reviewed source",
        kind="government",
        adapter="nbs",
        schedule_minutes=60,
    )
    session.add_all([situation, source])
    session.flush()
    document = EvidenceDocument(
        source_id=source.id,
        url="https://example.org/report",
        canonical_url="https://example.org/report",
        retrieved_at=NOW,
        published_at=NOW,
        content_sha256="c" * 64,
        storage_key=f"test/package-{suffix}",
        mime="text/html",
        title="Report",
    )
    permission = SourcePermission(
        source_id=source.id,
        version=1,
        may_collect=True,
        may_store_full_text=False,
        may_republish_numbers=True,
        approved_at=NOW,
        review_due_at=NOW + timedelta(days=30),
    )
    session.add_all([document, permission])
    session.flush()
    version = AssessmentVersion(
        situation_id=situation.id,
        version=1,
        template="T1_price_change",
        template_version="v1",
        policy_version="v1",
        inputs_hash="d" * 64,
        status="published",
        evidence_state=evidence_state,
        severity="medium",
        headline=f"Reported {topic} change",
        facts=[{"evidence_ids": [document.id]}],
        explanation="A reviewed report summarizes this change.",
        possible_factors=[],
        unknowns=[],
        scope_label="Nigeria",
        period_label="May 2026",
        published_at=NOW,
        valid_until=NOW + timedelta(days=1),
    )
    session.add(version)
    session.flush()
    situation.current_version_id = version.id
    session.flush()
    return situation, version, document, source


def _admin(session: Session):
    operator, _ = create_operator(
        session,
        f"package-admin-{uuid4().hex[:10]}@example.org",
        "correct horse battery staple",
        "admin",
    )
    return operator


def _campaign(session: Session, operator, *, category: str, fee: int | None):
    sponsor = create_sponsor(
        session,
        operator,
        SponsorDraft(
            public_name=f"Reviewed {category}",
            website_url="https://example.org",
            contact_email=f"contact-{uuid4().hex[:8]}@example.org",
            category=category,
        ),
    )
    approved_sponsor = transition_sponsor(
        session, operator, sponsor.id, status="approved", expected_revision=sponsor.revision
    )
    campaign = create_campaign(
        session,
        operator,
        CampaignDraft(
            sponsor_id=sponsor.id,
            internal_name=f"campaign-{uuid4().hex[:8]}",
            agreed_fee_minor=fee,
        ),
    )
    approved_campaign = transition_campaign(
        session,
        operator,
        campaign.id,
        status="approved",
        expected_revision=campaign.revision,
    )
    return approved_sponsor, approved_campaign


def _store_context(session: Session, version: AssessmentVersion):
    ref = content_ref_for(session, version.id)
    assert ref is not None
    decision = classify_content(session, content_ref=ref, now=NOW)
    assert decision.suitability == "eligible"
    return ref, store_context_decision(session, input_ref=ref, decision=decision, now=NOW)


def test_matching_uses_only_current_context_and_curated_category(session: Session) -> None:
    operator = _admin(session)
    energy_sponsor, _ = _campaign(session, operator, category="energy_provider", fee=25_000)
    food_sponsor, _ = _campaign(session, operator, category="food_retailer", fee=25_000)
    _, version, _, _ = _content(session, topic="energy")
    ref, _ = _store_context(session, version)

    result = match_sponsors(session, content_ref=ref, at=NOW)

    assert result.context_eligible is True
    by_id = {proposal.sponsor_id: proposal for proposal in result.proposals}
    assert by_id[energy_sponsor.id].matched is True
    assert "curated_category_energy_provider" in by_id[energy_sponsor.id].reason_codes
    assert by_id[food_sponsor.id].matched is False
    assert by_id[food_sponsor.id].reason_codes == (
        "approved_sponsor",
        "curated_category_mismatch",
    )
    assert not hasattr(by_id[energy_sponsor.id], "fit_score")
    assert "contact_email" not in by_id[energy_sponsor.id].model_dump()


def test_package_draft_snapshots_current_inventory_and_known_fee_without_booking(
    session: Session,
) -> None:
    operator = _admin(session)
    _sponsor, campaign = _campaign(session, operator, category="energy_provider", fee=125_000)
    _, version, _, _ = _content(session)
    _ref, context = _store_context(session, version)
    controls = session.get(CommercialControl, 1)
    assert controls is not None
    request = PackageRequest(
        campaign_id=campaign.id,
        topic="energy",
        starts_at=NOW + timedelta(days=2),
        ends_at=NOW + timedelta(days=3),
        expected_campaign_revision=campaign.revision,
        expected_controls_revision=controls.revision,
    )

    draft = create_package_draft(session, operator, request, at=NOW + timedelta(minutes=1))
    duplicate = create_package_draft(session, operator, request, at=NOW + timedelta(minutes=2))

    assert draft.id == duplicate.id
    assert draft.status == "draft"
    assert draft.facts["inventory_status"] == "available_at_draft_time"
    assert draft.facts["price_status"] == "known"
    assert draft.facts["agreed_fee_minor"] == 125_000
    assert draft.input_refs["contexts"] == [
        {
            "context_id": context.id,
            "context_revision": context.revision,
            "assessment_version_id": version.id,
            "content_hash": context.content_hash,
        }
    ]
    assert session.scalars(select(PlacementBooking)).all() == []

    approved = review_package_draft(
        session,
        operator,
        draft.id,
        status="approved",
        expected_revision=draft.revision,
        at=NOW + timedelta(minutes=3),
    )
    assert approved.status == "approved"
    assert approved.revision == draft.revision + 1
    assert session.scalars(select(PlacementBooking)).all() == []


def test_unknown_price_remains_unknown_and_cannot_be_approved(session: Session) -> None:
    operator = _admin(session)
    _sponsor, campaign = _campaign(session, operator, category="energy_provider", fee=None)
    _, version, _, _ = _content(session)
    _store_context(session, version)
    controls = session.get(CommercialControl, 1)
    assert controls is not None
    draft = create_package_draft(
        session,
        operator,
        PackageRequest(
            campaign_id=campaign.id,
            topic="energy",
            starts_at=NOW + timedelta(days=2),
            ends_at=NOW + timedelta(days=3),
            expected_campaign_revision=campaign.revision,
            expected_controls_revision=controls.revision,
        ),
        at=NOW + timedelta(minutes=1),
    )

    assert draft.facts["price_status"] == "unknown"
    assert draft.facts["agreed_fee_minor"] is None
    with pytest.raises(PackageError, match="price is unknown"):
        review_package_draft(
            session,
            operator,
            draft.id,
            status="approved",
            expected_revision=draft.revision,
            at=NOW + timedelta(minutes=2),
        )


def test_permission_change_invalidates_projection_and_stale_worker_result_is_discarded(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from africasignal.jobs.handlers import commercial as commercial_handler
    from africasignal.models import CommercialControl
    from africasignal.sources.console import PermissionInput, publish_permission_version

    operator = _admin(session)
    _situation, version, _document, source = _content(session)
    ref, context = _store_context(session, version)
    publish_permission_version(
        session,
        operator,
        source.id,
        PermissionInput(
            may_collect=True,
            may_store_full_text=False,
            max_quote_chars=None,
            may_republish_numbers=False,
            link_required=False,
            retention_days=None,
            terms_url="https://example.org/terms",
            rights_basis="Reviewed public terms.",
        ),
        terms_reviewed=True,
        now=NOW + timedelta(minutes=1),
    )
    session.refresh(session.get(ContentContext, context.id))
    invalidated = session.get(ContentContext, context.id)
    assert invalidated is not None
    assert invalidated.invalidated_at == NOW + timedelta(minutes=1)
    assert invalidated.invalidation_reason == "source_permission_changed"

    controls = session.get(CommercialControl, 1)
    workload = session.get(WorkloadControl, "commercial")
    assert controls is not None and workload is not None
    controls.global_enabled = True
    workload.enabled = True
    workload.schedule = {
        "timezone": "Africa/Lagos",
        "windows": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "23:59"}],
        "max_concurrency": 1,
        "max_items_per_run": 10,
    }
    monkeypatch.setattr(
        commercial_handler, "get_settings", lambda: SimpleNamespace(commercial_deny=False)
    )

    stale_job = ClaimedJob(
        id=1,
        kind="commercial_context_rebuild",
        payload={"assessment_version_id": version.id, "content_hash": "0" * 64},
        attempts=1,
        max_attempts=3,
    )
    commercial_handler.rebuild_commercial_context(
        JobContext(session=session, job=stale_job, worker_id="test")
    )
    assert (
        session.scalars(
            select(ContentContext).where(
                ContentContext.assessment_version_id == version.id,
                ContentContext.content_hash != ref.content_hash,
            )
        ).all()
        == []
    )

    current_ref = content_ref_for(session, version.id)
    assert current_ref is not None
    current_job = ClaimedJob(
        id=2,
        kind="commercial_context_rebuild",
        payload={
            "assessment_version_id": version.id,
            "content_hash": current_ref.content_hash,
        },
        attempts=1,
        max_attempts=3,
    )
    commercial_handler.rebuild_commercial_context(
        JobContext(session=session, job=current_job, worker_id="test")
    )
    rebuilt = session.scalar(
        select(ContentContext)
        .where(
            ContentContext.assessment_version_id == version.id,
            ContentContext.content_hash == current_ref.content_hash,
            ContentContext.invalidated_at.is_(None),
        )
        .order_by(ContentContext.revision.desc())
    )
    assert rebuilt is not None
    assert rebuilt.suitability == "unknown"
    assert rebuilt.reason_codes == ["permission_denied"]
