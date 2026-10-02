from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from africasignal.models import (
    AssessmentVersion,
    ContentContext,
    EvidenceDocument,
    Place,
    Setting,
    Situation,
    Source,
    SourcePermission,
)
from africasignal.operations.commercial_context import (
    classify_content,
    content_ref_for,
    store_context_decision,
)

NOW = datetime.now(UTC)


def _content(
    session: Session,
    *,
    evidence_state: str = "corroborated",
    status: str = "published",
    severity: str = "high",
    may_republish: bool = True,
) -> tuple[Situation, AssessmentVersion, EvidenceDocument]:
    suffix = uuid4().hex[:10]
    place = Place(kind="country", name="Nigeria", code=f"NG-CTX-{suffix}")
    session.add(place)
    session.flush()
    situation = Situation(
        slug=f"context-test-{suffix}",
        kind="price_series",
        topic="energy",
        title="Petrol price",
        item_code="pms_litre",
        place_id=place.id,
        status="active",
    )
    session.add(situation)
    source = Source(
        slug=f"context-test-source-{suffix}",
        name="Context test source",
        kind="government",
        adapter="nbs",
        schedule_minutes=60,
    )
    session.add(source)
    session.flush()
    document = EvidenceDocument(
        source_id=source.id,
        url="https://example.org/report",
        canonical_url="https://example.org/report",
        retrieved_at=NOW,
        published_at=NOW,
        content_sha256="a" * 64,
        storage_key="test/context-report",
        mime="text/html",
        title="Report",
    )
    session.add(document)
    session.flush()
    session.add(
        SourcePermission(
            source_id=source.id,
            version=1,
            may_collect=True,
            may_store_full_text=False,
            may_republish_numbers=may_republish,
            approved_at=NOW,
            review_due_at=NOW + timedelta(days=30),
        )
    )
    version = AssessmentVersion(
        situation_id=situation.id,
        version=1,
        template="T1_price_change",
        template_version="v1",
        policy_version="v1",
        inputs_hash="b" * 64,
        status=status,
        evidence_state=evidence_state,
        severity=severity,
        headline="Petrol price change",
        facts=[{"evidence_ids": [document.id]}],
        explanation="A public report summarizes this change.",
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
    return situation, version, document


def test_deterministic_context_is_content_only_and_revision_bound(session: Session) -> None:
    _, version, document = _content(session)
    content_ref = content_ref_for(session, version.id)
    assert content_ref is not None

    decision = classify_content(session, content_ref=content_ref, now=NOW)
    assert decision.suitability == "eligible"
    assert decision.topic_tags == ("energy",)
    assert decision.canonical_place_id > 0
    assert decision.reason_codes == ("current_public",)

    stored = store_context_decision(session, input_ref=content_ref, decision=decision, now=NOW)
    duplicate = store_context_decision(
        session, input_ref=content_ref, decision=decision, now=NOW + timedelta(minutes=1)
    )
    assert stored.id == duplicate.id and stored.revision == 1

    document.status = "withdrawn"
    session.flush()
    declined = classify_content(session, content_ref=content_ref, now=NOW + timedelta(minutes=2))
    assert declined.suitability == "unknown"
    assert declined.reason_codes == ("withdrawn_evidence",)
    rebuilt = store_context_decision(
        session, input_ref=content_ref, decision=declined, now=NOW + timedelta(minutes=2)
    )
    assert rebuilt.revision == 2
    assert session.get(ContentContext, stored.id).suitability == "eligible"


def test_stale_withdrawn_disputed_and_permission_denied_context_is_ineligible(
    session: Session,
) -> None:
    _, stale, _ = _content(session, status="stale")
    stale_ref = content_ref_for(session, stale.id)
    assert stale_ref is not None
    assert classify_content(session, content_ref=stale_ref, now=NOW).suitability == "unknown"

    _, disputed, _ = _content(session, evidence_state="disputed")
    disputed_ref = content_ref_for(session, disputed.id)
    assert disputed_ref is not None
    decision = classify_content(session, content_ref=disputed_ref, now=NOW)
    assert decision.suitability == "unknown"
    assert decision.reason_codes == ("disputed_evidence",)

    _, permission_denied, _ = _content(session, may_republish=False)
    permission_ref = content_ref_for(session, permission_denied.id)
    assert permission_ref is not None
    denied = classify_content(session, content_ref=permission_ref, now=NOW)
    assert denied.suitability == "unknown"
    assert denied.reason_codes == ("permission_denied",)

    _, withdrawn, _ = _content(session, status="withdrawn")
    withdrawn_ref = content_ref_for(session, withdrawn.id)
    assert withdrawn_ref is not None
    assert classify_content(session, content_ref=withdrawn_ref, now=NOW).reason_codes == (
        "withdrawn_content",
    )


def test_publication_suspension_and_content_hash_changes_fail_closed(session: Session) -> None:
    _, version, _ = _content(session)
    content_ref = content_ref_for(session, version.id)
    assert content_ref is not None
    setting = session.get(Setting, "publication_suspended")
    if setting is None:
        session.add(Setting(key="publication_suspended", value=True))
    else:
        setting.value = True
    session.flush()
    decision = classify_content(session, content_ref=content_ref, now=NOW)
    assert decision.suitability == "unknown"
    assert decision.reason_codes == ("publication_suspended",)

    version.headline = "Corrected headline"
    session.flush()
    assert content_ref_for(session, version.id) != content_ref
    from africasignal.operations.commercial_context import ContextError

    with pytest.raises(ContextError, match="hash is stale"):
        classify_content(session, content_ref=content_ref, now=NOW)
