"""Deterministic multi-assessment selection and current-rights invalidation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.assess.insight_candidates import refresh_and_generate
from africasignal.models import (
    AssessmentInput,
    AssessmentVersion,
    EvidenceDocument,
    InsightCandidate,
    InsightCandidateAssessment,
    Operator,
    Outbox,
    Place,
    Situation,
    SocialPublication,
    Source,
    SourcePermission,
)
from africasignal.operations.insight_candidates import review

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def _source(session: Session) -> Source:
    source = Source(
        slug="candidate-source",
        name="Candidate source",
        kind="official_statistics",
        adapter="nbs",
        schedule_minutes=30,
    )
    session.add(source)
    session.flush()
    session.add(
        SourcePermission(
            source_id=source.id,
            version=1,
            may_collect=True,
            may_store_full_text=False,
            may_republish_numbers=True,
            link_required=True,
            retention_days=365,
            terms_url="https://source.example/terms",
            rights_basis="approved public statistical reuse",
            approved_at=NOW - timedelta(days=1),
        )
    )
    session.flush()
    return source


def _assessment(
    session: Session,
    source: Source,
    place: Place,
    *,
    item_code: str,
    change: float,
    evidence_state: str = "reported",
    period: str = "September 2026",
    situation: Situation | None = None,
    version_number: int = 1,
    supersedes_id: int | None = None,
) -> AssessmentVersion:
    if situation is None:
        situation = Situation(
            slug=f"{item_code}-{place.code}",
            kind="price_series",
            topic="energy",
            title=f"{item_code} price in {place.name}",
            item_code=item_code,
            place_id=place.id,
            status="active",
        )
        session.add(situation)
        session.flush()
    document = EvidenceDocument(
        source_id=source.id,
        url=f"https://source.example/{item_code}-{period.replace(' ', '-').lower()}",
        canonical_url=f"https://source.example/{item_code}-{period.replace(' ', '-').lower()}",
        retrieved_at=NOW - timedelta(days=1),
        published_at=NOW - timedelta(days=2),
        content_sha256=hashlib.sha256(f"{item_code}-{period}".encode()).hexdigest(),
        storage_key=f"test/{item_code}-{period.replace(' ', '-')}",
        mime="text/html",
        title=f"{item_code} release",
        status="active",
    )
    session.add(document)
    session.flush()
    version = AssessmentVersion(
        situation_id=situation.id,
        version=version_number,
        template="T1_price_change",
        template_version="t1",
        policy_version="p1",
        inputs_hash=f"input-{item_code}-{place.code}-{period}-{evidence_state}",
        status="published",
        evidence_state=evidence_state,
        severity="medium",
        headline=f"{item_code} changed by {change}%",
        facts=[
            {
                "label": "Month-on-month change",
                "value": change,
                "unit": "%",
                "period": period,
                "place_code": place.code,
                "evidence_ids": [document.id],
            },
            {
                "label": "Official statements that disagree",
                "value": 1 if evidence_state == "disputed" else 0,
                "unit": "statements",
                "period": period,
                "place_code": place.code,
                "evidence_ids": [document.id],
            },
        ],
        explanation_assertions=[],
        possible_factors=[],
        unknowns=["No cause is established by this comparison."],
        scope_label=place.name,
        period_label=period,
        last_checked_at=NOW,
        valid_until=NOW + timedelta(days=14),
        change_summary="Updated with September data",
        published_at=NOW,
        supersedes_id=supersedes_id,
    )
    session.add(version)
    session.flush()
    session.add(
        AssessmentInput(
            assessment_version_id=version.id,
            input_kind="evidence_document",
            input_id=document.id,
        )
    )
    situation.current_version_id = version.id
    session.flush()
    return version


def test_selector_builds_deduped_comparable_and_disagreement_packets_then_invalidates(
    session: Session,
) -> None:
    country = Place(kind="country", code="NG", name="Nigeria")
    session.add(country)
    session.flush()
    source = _source(session)
    rice = _assessment(session, source, country, item_code="rice", change=12.0)
    fuel = _assessment(session, source, country, item_code="fuel", change=1.0)
    disputed = _assessment(
        session,
        source,
        country,
        item_code="electricity",
        change=2.0,
        evidence_state="disputed",
    )
    incompatible = _assessment(
        session, source, country, item_code="transport", change=40.0, period="August 2026"
    )

    result = refresh_and_generate(session, NOW)

    assert result == {"created": 5, "marked_stale": 0}
    candidates = list(session.scalars(select(InsightCandidate)))
    comparisons = [row for row in candidates if row.candidate_type == "multi_series_comparison"]
    disagreements = [row for row in candidates if row.candidate_type == "evidence_disagreement"]
    assert len(comparisons) == 1
    assert len(disagreements) == 1
    comparison_ids = set(
        session.scalars(
            select(InsightCandidateAssessment.assessment_version_id).where(
                InsightCandidateAssessment.candidate_id == comparisons[0].id
            )
        )
    )
    assert comparison_ids == {rice.id, fuel.id}
    assert incompatible.id not in comparison_ids and disputed.id not in comparison_ids
    assert (
        "difference_at_least_5_percentage_points"
        in comparisons[0].packet["brief"]["significance_reason"]
    )
    assert (
        comparisons[0]
        .packet["brief"]["interpretation_limit"]
        .startswith("This compares measured price changes")
    )
    assert comparisons[0].packet["source_coverage"][0]["document_links"]
    assert len(comparisons[0].packet["source_coverage"]) == 1
    operator = Operator(
        email="candidate-reviewer@example.test",
        password_hash="test-only",
        totp_secret_enc="test-only",
        role="admin",
    )
    session.add(operator)
    session.flush()
    review(
        session,
        operator,
        comparisons[0].id,
        "selected",
        "Selected for a comparable period-bound editorial review.",
        usefulness_rating=4,
        scope_correct=True,
        unsupported_critical=False,
        now=NOW,
    )
    assert comparisons[0].status == "selected"
    assert session.scalars(select(Outbox)).all() == []
    assert session.scalars(select(SocialPublication)).all() == []
    assert refresh_and_generate(session, NOW + timedelta(minutes=15)) == {
        "created": 0,
        "marked_stale": 0,
    }

    session.add(
        SourcePermission(
            source_id=source.id,
            version=2,
            may_collect=False,
            may_store_full_text=False,
            may_republish_numbers=False,
            link_required=True,
            retention_days=365,
            terms_url="https://source.example/terms",
            rights_basis="reuse permission withdrawn",
            approved_at=NOW + timedelta(minutes=20),
        )
    )
    session.flush()
    updated = refresh_and_generate(session, NOW + timedelta(minutes=21))
    assert updated["marked_stale"] == len(candidates)
    assert all(row.status == "stale" for row in candidates)


def test_selector_recognizes_only_a_consecutive_same_direction_material_trend(
    session: Session,
) -> None:
    country = Place(kind="country", code="NG", name="Nigeria")
    session.add(country)
    session.flush()
    source = _source(session)
    prior = _assessment(
        session, source, country, item_code="rice", change=7.0, period="August 2026"
    )
    prior.status = "superseded"
    situation = session.get(Situation, prior.situation_id)
    assert situation is not None
    current = _assessment(
        session,
        source,
        country,
        item_code="rice",
        change=9.0,
        period="September 2026",
        situation=situation,
        version_number=2,
        supersedes_id=prior.id,
    )
    session.flush()

    result = refresh_and_generate(session, NOW)

    assert result["created"] == 2  # material change and persistent trend
    trend = session.scalar(
        select(InsightCandidate).where(InsightCandidate.candidate_type == "persistent_trend")
    )
    assert trend is not None
    members = session.execute(
        select(InsightCandidateAssessment.role, InsightCandidateAssessment.assessment_version_id)
        .where(InsightCandidateAssessment.candidate_id == trend.id)
        .order_by(InsightCandidateAssessment.ordinal)
    ).all()
    assert members == [("prior", prior.id), ("current", current.id)]
    assert trend.packet["brief"]["trend"] == [
        {"period": "August 2026", "value": "7.0", "unit": "%"},
        {"period": "September 2026", "value": "9.0", "unit": "%"},
    ]
