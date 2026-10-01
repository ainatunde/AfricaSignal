"""T2 policy situations from real claims (AS-027), on PostgreSQL."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.evidence.origins import assign_origin
from africasignal.jobs.handlers.assess_situation import assess_situation as assess_job
from africasignal.models import (
    AssessmentInput,
    AssessmentVersion,
    Claim,
    EvidenceDocument,
    Job,
    Place,
    Situation,
    Source,
)
from africasignal.publish import hooks
from africasignal.publish.claim_assessments import request_claim_assessments
from africasignal.publish.invalidation import plan_corrections
from africasignal.publish.policy_situations import (
    ensure_policy_situations,
    policy_slug,
)
from africasignal.publish.situations import assess_situation
from africasignal.publish.versions import apply_policy, release_held
from tests.integration.outlet_support import approved_source
from tests.integration.test_origins import WIRE_STORY, _doc

IKEJA = "electricity_tariff_band_a:ikeja-electric"
EKO = "electricity_tariff_band_a:eko"
NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
ORDER = (
    "Nigerian Electricity Regulatory Commission. Order on the September 2026 multi-year tariff "
    "order for Ikeja Electric. Band A customers will pay a tariff of N209.50 per kWh from "
    "1 September 2026, up from N200.00 per kWh in August 2026, following the review of costs."
)


def vetted(session: Session, slug: str, **kwargs: Any) -> Source:
    """A source approved long enough before the assessment clock to be trusted."""
    return approved_source(session, slug, now=NOW, **kwargs)


@pytest.fixture
def places(session: Session) -> dict[str, int]:
    ids = {}
    country = Place(kind="country", name="Nigeria", code="NG")
    session.add(country)
    session.flush()
    ids["NG"] = country.id
    lagos = Place(kind="state", name="Lagos", code="NG-LA", parent_id=country.id)
    session.add(lagos)
    session.flush()
    ids["NG-LA"] = lagos.id
    return ids


@pytest.fixture
def nerc(session: Session) -> Source:
    source = vetted(session, "nerc", kind="regulator")
    source.owner = "Nigerian Electricity Regulatory Commission"
    session.flush()
    return source


@pytest.fixture
def outlet(session: Session) -> Source:
    return vetted(session, "punch")


def order(
    session: Session,
    nerc: Source,
    value: str,
    effective: date,
    *,
    series: str = IKEJA,
    text: str = ORDER,
    published: datetime = datetime(2026, 8, 28, 9, tzinfo=UTC),
    claim_text: str | None = None,
    doc: EvidenceDocument | None = None,
) -> Claim:
    """A rate a primary document gives."""
    doc = doc or _doc(session, nerc, text, when=published)
    assign_origin(session, doc)
    claim = Claim(
        evidence_document_id=doc.id,
        claim_type="policy_statement",
        text=claim_text or f"Band A tariff is N{value} per kWh.",
        passage=(claim_text or f"Band A tariff is N{value} per kWh").rstrip("."),
        policy_series=series,
        stated_value=Decimal(value),
        stated_unit="NGN/kWh",
        direction="unknown",
        occurred_from=effective,
        time_precision="month",
        extractor_version="test",
        valid=True,
    )
    session.add(claim)
    session.flush()
    return claim


def report(
    session: Session,
    outlet: Source,
    *,
    value: str | None = None,
    text: str = WIRE_STORY,
    claim_text: str = "Customers are now being charged the new Band A rate.",
    when: datetime = datetime(2026, 9, 10, 9, tzinfo=UTC),
) -> Claim:
    doc = _doc(session, outlet, text, when=when)
    assign_origin(session, doc)
    claim = Claim(
        evidence_document_id=doc.id,
        claim_type="policy_statement",
        text=claim_text,
        passage=claim_text.rstrip("."),
        policy_series=IKEJA,
        stated_value=None if value is None else Decimal(value),
        direction="unknown",
        time_precision="unknown",
        extractor_version="test",
        valid=True,
    )
    session.add(claim)
    session.flush()
    return claim


def situation(session: Session, series: str = IKEJA, place: str = "NG-LA") -> Situation:
    return session.scalars(
        select(Situation).where(Situation.slug == policy_slug(series, place))
    ).one()


def assess(session: Session, sit: Situation) -> AssessmentVersion:
    outcome = assess_situation(session, sit.id, NOW)
    assert outcome.version is not None, outcome
    return outcome.version


def inputs_of(session: Session, version: AssessmentVersion, kind: str) -> set[int]:
    return set(
        session.scalars(
            select(AssessmentInput.input_id).where(
                AssessmentInput.assessment_version_id == version.id,
                AssessmentInput.input_kind == kind,
            )
        )
    )


# --- situations (B8.1) -------------------------------------------------------------------------


def test_situations_follow_config_policies_yaml(session: Session, places: dict[str, int]) -> None:
    found = ensure_policy_situations(session)
    assert sorted(s.slug for s in found) == [
        "policy-electricity_tariff_band_a-eko-ng-la",
        "policy-electricity_tariff_band_a-ikeja-electric-ng-la",
        "policy-pms_regulated_price-ng",
    ]
    ikeja = situation(session)
    assert (ikeja.kind, ikeja.topic, ikeja.status) == ("policy", "energy", "active")
    assert ikeja.policy_series == IKEJA and ikeja.place_id == places["NG-LA"]
    assert ikeja.item_code is None and ikeja.current_version_id is None
    assert ikeja.title == "Electricity tariff, Band A, Ikeja Electric"
    assert situation(session, "pms_regulated_price", "NG").place_id == places["NG"]


def test_creating_situations_twice_changes_nothing(
    session: Session, places: dict[str, int]
) -> None:
    first = {s.id for s in ensure_policy_situations(session)}
    assert {s.id for s in ensure_policy_situations(session)} == first


def test_only_the_asked_for_series_and_only_places_that_exist(
    session: Session, places: dict[str, int]
) -> None:
    assert [s.slug for s in ensure_policy_situations(session, {EKO})] == [
        "policy-electricity_tariff_band_a-eko-ng-la"
    ]
    session.execute(Situation.__table__.delete())
    session.execute(Place.__table__.delete().where(Place.code == "NG-LA"))
    assert ensure_policy_situations(session, {IKEJA}) == []  # Lagos is not loaded


# --- assessments -------------------------------------------------------------------------------


def test_a_primary_document_gives_a_reported_attributed_assessment(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    now_rate = order(session, nerc, "209.50", date(2026, 9, 1))
    before = order(session, nerc, "200.00", date(2026, 8, 1), doc=None, text=ORDER + " (Aug)")
    ensure_policy_situations(session, {IKEJA})
    version = assess(session, situation(session))
    assert (version.template, version.template_version) == ("T2_policy_change", "T2-2")
    assert version.status == "draft" and version.policy_version == "unapplied"
    assert version.evidence_state == "reported" and version.severity == "none"  # 4.8 % < 5 %
    assert version.headline == (
        "Electricity tariff, Band A, Ikeja Electric: NERC says the rate rose 4.8% "
        "to ₦209.50 per kWh from 1 September 2026"
    )
    assert version.scope_label == "Lagos State (Band A customers of Ikeja Electric)"
    assert version.period_label == "from 1 September 2026"
    assert inputs_of(session, version, "claim") == {now_rate.id, before.id}
    assert inputs_of(session, version, "evidence_document") == {
        now_rate.evidence_document_id,
        before.evidence_document_id,
    }
    assert version.valid_until == datetime(2026, 10, 12, 23, 59, 59, tzinfo=UTC)
    assert assess_situation(session, situation(session).id, NOW).outcome == "unchanged"


def test_with_no_claims_there_is_nothing_to_assess(
    session: Session, places: dict[str, int]
) -> None:
    ensure_policy_situations(session, {IKEJA})
    outcome = assess_situation(session, situation(session).id, NOW)
    assert (outcome.outcome, outcome.reason) == ("skipped", "no usable policy claims")


def test_claims_of_another_series_are_not_used(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    order(session, nerc, "250.00", date(2026, 9, 1), series=EKO, text=ORDER + " Eko")
    order(session, nerc, "209.50", date(2026, 9, 1))
    ensure_policy_situations(session, {IKEJA, EKO})
    version = assess(session, situation(session))
    assert version.facts[0]["value"] == 209.5
    assert version.period_label == "from 1 September 2026"


def test_news_that_the_rate_is_charged_corroborates(
    session: Session, places: dict[str, int], nerc: Source, outlet: Source
) -> None:
    order(session, nerc, "209.50", date(2026, 9, 1))
    order(session, nerc, "200.00", date(2026, 8, 1), text=ORDER + " (Aug)")
    news = report(session, outlet)
    ensure_policy_situations(session, {IKEJA})
    version = assess(session, situation(session))
    assert version.evidence_state == "corroborated"
    assert news.id in inputs_of(session, version, "claim")
    assert "No independent report that this rate is being applied" not in version.unknowns


def test_a_news_reprint_of_the_order_does_not_corroborate(
    session: Session, places: dict[str, int], nerc: Source, outlet: Source
) -> None:
    order(session, nerc, "209.50", date(2026, 9, 1))
    order(session, nerc, "200.00", date(2026, 8, 1), text=ORDER + " (Aug)")
    report(session, outlet, text=ORDER + " (Punch)", claim_text="Customers are now being charged.")
    ensure_policy_situations(session, {IKEJA})
    assert assess(session, situation(session)).evidence_state == "reported"


def test_syndicated_copies_of_the_report_are_one_origin(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    order(session, nerc, "209.50", date(2026, 9, 1))
    order(session, nerc, "200.00", date(2026, 8, 1), text=ORDER + " (Aug)")
    for slug in ("punch", "vanguard", "thisday"):
        report(session, vetted(session, slug), text=WIRE_STORY + " (Reuters)")
    ensure_policy_situations(session, {IKEJA})
    version = assess(session, situation(session))
    fact = next(f for f in version.facts if f["label"].startswith("Independent reports"))
    assert version.evidence_state == "corroborated" and fact["value"] == 1


def test_a_later_suspension_disputes(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    order(session, nerc, "209.50", date(2026, 9, 1))
    order(session, nerc, "200.00", date(2026, 8, 1), text=ORDER + " (Aug)")
    suspension = order(
        session,
        nerc,
        "209.50",
        date(2026, 9, 1),
        text="NERC suspends the September 2026 Band A tariff order pending review.",
        published=datetime(2026, 9, 15, 9, tzinfo=UTC),
        claim_text="NERC suspends the new Band A tariff.",
    )
    ensure_policy_situations(session, {IKEJA})
    version = assess(session, situation(session))
    assert version.evidence_state == "disputed"
    assert suspension.id in inputs_of(session, version, "claim")


def test_news_only_is_insufficient_evidence_under_rule_r5(
    session: Session, places: dict[str, int], outlet: Source
) -> None:
    hooks.clear_publication_hooks()
    report(session, outlet, value="209.50", claim_text="Ikeja Electric's Band A rate is N209.50.")
    ensure_policy_situations(session, {IKEJA})
    sit = situation(session)
    version = assess(session, sit)
    decision = apply_policy(session, version.id, NOW)
    assert decision is not None and decision.reasons == ("R5",) and decision.insufficient_card
    assert version.status == "published" and sit.current_version_id == version.id
    assert version.severity == "none" and version.explanation is None
    assert version.facts[0]["label"].startswith("Reported by ")


def test_a_primary_document_is_not_an_r5_case(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    hooks.clear_publication_hooks()
    order(session, nerc, "209.50", date(2026, 9, 1))
    order(session, nerc, "200.00", date(2026, 8, 1), text=ORDER + " (Aug)")
    ensure_policy_situations(session, {IKEJA})
    version = assess(session, situation(session))
    decision = apply_policy(session, version.id, NOW)
    assert decision is not None and decision.status == "published" and decision.reasons == ()


def test_a_high_severity_first_version_is_held_then_released(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    hooks.clear_publication_hooks()
    order(session, nerc, "300.00", date(2026, 9, 1))
    order(session, nerc, "200.00", date(2026, 8, 1), text=ORDER + " (Aug)")
    ensure_policy_situations(session, {IKEJA})
    version = assess(session, situation(session))
    assert version.severity == "high"
    decision = apply_policy(session, version.id, NOW)
    assert decision is not None and decision.status == "held" and decision.reasons == ("R7",)
    release_held(session, NOW + timedelta(minutes=61))
    assert version.status == "published"


def test_a_new_rate_makes_version_two_and_supersedes_version_one(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    hooks.clear_publication_hooks()
    order(session, nerc, "209.50", date(2026, 9, 1))
    order(session, nerc, "200.00", date(2026, 8, 1), text=ORDER + " (Aug)")
    ensure_policy_situations(session, {IKEJA})
    sit = situation(session)
    first = assess(session, sit)
    apply_policy(session, first.id, NOW)
    order(session, nerc, "215.00", date(2026, 9, 18), text=ORDER + " (Sep 18)")
    second = assess(session, sit)
    assert second.version == 2
    assert second.change_summary == (
        "Updated: the rate now in force is from 18 September 2026 (previously from 1 September 2026)"
    )
    apply_policy(session, second.id, NOW)
    assert first.status == "superseded" and sit.current_version_id == second.id


def test_an_invalidated_claim_is_planned_as_a_correction(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    hooks.clear_publication_hooks()
    rate = order(session, nerc, "209.50", date(2026, 9, 1))
    order(session, nerc, "200.00", date(2026, 8, 1), text=ORDER + " (Aug)")
    ensure_policy_situations(session, {IKEJA})
    sit = situation(session)
    version = assess(session, sit)
    apply_policy(session, version.id, NOW)
    rate.valid = False
    session.flush()
    assert plan_corrections(session, "claim", [rate.id]) == {
        sit.id: "Corrected: a report behind this assessment was found to be invalid"
    }


# --- the job and the triggers ------------------------------------------------------------------


def jobs(session: Session) -> list[Job]:
    return list(session.scalars(select(Job).where(Job.kind == "assess_situation").order_by(Job.id)))


def test_the_assess_job_handles_policy_situations(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    hooks.clear_publication_hooks()
    order(session, nerc, "209.50", date(2026, 9, 1))
    order(session, nerc, "200.00", date(2026, 8, 1), text=ORDER + " (Aug)")
    ensure_policy_situations(session, {IKEJA})
    sit = situation(session)
    job = SimpleNamespace(id=1, payload={"situation_id": sit.id})
    assess_job(SimpleNamespace(session=session, job=job))  # type: ignore[arg-type]
    assert sit.current_version_id is not None


def test_policy_claims_create_their_situation_and_queue_its_assessment(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    claim = order(session, nerc, "209.50", date(2026, 9, 1))
    assert session.scalars(select(Situation)).all() == []
    job_ids = request_claim_assessments(session, claim.evidence_document_id)
    sit = situation(session)
    assert len(job_ids) == 1
    assert [j.payload["situation_id"] for j in jobs(session)] == [sit.id]
    assert request_claim_assessments(session, claim.evidence_document_id) == []  # deduplicated


def test_invalid_policy_claims_create_nothing(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    claim = order(session, nerc, "209.50", date(2026, 9, 1))
    claim.valid = False
    session.flush()
    assert request_claim_assessments(session, claim.evidence_document_id) == []
    assert session.scalars(select(Situation)).all() == []


def test_the_situation_of_a_series_outside_the_config_is_not_created(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    claim = order(session, nerc, "209.50", date(2026, 9, 1), series="unlisted_series")
    assert request_claim_assessments(session, claim.evidence_document_id) == []


def test_every_fact_is_backed_by_active_documents(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    order(session, nerc, "209.50", date(2026, 9, 1))
    order(session, nerc, "200.00", date(2026, 8, 1), text=ORDER + " (Aug)")
    ensure_policy_situations(session, {IKEJA})
    version = assess(session, situation(session))
    active = set(
        session.scalars(select(EvidenceDocument.id).where(EvidenceDocument.status == "active"))
    )
    assert all(f["evidence_ids"] and set(f["evidence_ids"]) <= active for f in version.facts)


# --- look-alike sources (security review S-08) -------------------------------------------------


def _stories(n: int) -> list[str]:
    """Different wording each time, so SimHash clustering cannot merge them into one origin."""
    return [
        f"Report {i}: " + " ".join(f"{word}{i}x{j}" for j, word in enumerate(["tariff"] * 60))
        for i in range(n)
    ]


def _two_orders(session: Session, nerc: Source) -> None:
    order(session, nerc, "209.50", date(2026, 9, 1))
    order(session, nerc, "200.00", date(2026, 8, 1), text=ORDER + " (Aug)")


def test_a_swarm_of_unapproved_look_alike_sites_does_not_corroborate(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    _two_orders(session, nerc)
    for i, story in enumerate(_stories(6)):
        fake = approved_source(
            session, f"fake-{i}", now=NOW, approved_days_ago=None, home_url=f"https://fake{i}.ng"
        )
        report(session, fake, text=story)
    ensure_policy_situations(session, {IKEJA})
    version = assess(session, situation(session))
    assert version.evidence_state == "reported"
    assert "No independent report that this rate is being applied" in version.unknowns


def test_outlets_inside_the_probation_period_do_not_corroborate(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    _two_orders(session, nerc)
    for i, story in enumerate(_stories(3)):
        new = vetted(session, f"new-{i}", approved_days_ago=29, home_url=f"https://new{i}.ng")
        report(session, new, text=story)
    ensure_policy_situations(session, {IKEJA})
    assert assess(session, situation(session)).evidence_state == "reported"


def test_outlets_of_one_owner_count_as_one_voice(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    _two_orders(session, nerc)
    for i, story in enumerate(_stories(4)):
        site = vetted(
            session, f"network-{i}", owner="Shady Media Ltd", home_url=f"https://net{i}.example.ng"
        )
        report(session, site, text=story)
    ensure_policy_situations(session, {IKEJA})
    version = assess(session, situation(session))
    fact = next(f for f in version.facts if f["label"].startswith("Independent reports"))
    assert version.evidence_state == "corroborated" and fact["value"] == 1


def test_separate_outlets_count_separately_and_are_named(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    _two_orders(session, nerc)
    for slug, story in zip(("punch", "vanguard"), _stories(2), strict=True):
        report(
            session,
            vetted(session, slug, owner=f"{slug} publishers", home_url=f"https://{slug}.ng"),
            text=story,
        )
    ensure_policy_situations(session, {IKEJA})
    version = assess(session, situation(session))
    fact = next(f for f in version.facts if f["label"].startswith("Independent reports"))
    assert fact["value"] == 2
    assert sorted(fact["outlets"]) == ["Punch", "Vanguard"]
    assert fact["source_label"] == "Punch, Vanguard"


def test_an_unapproved_regulator_cannot_suspend_the_order(
    session: Session, places: dict[str, int], nerc: Source
) -> None:
    _two_orders(session, nerc)
    rogue = approved_source(session, "rogue-gov", kind="regulator", now=NOW, approved_days_ago=None)
    order(
        session,
        rogue,
        "209.50",
        date(2026, 9, 1),
        text="NERC suspends the September 2026 Band A tariff order pending review.",
        published=datetime(2026, 9, 15, 9, tzinfo=UTC),
        claim_text="NERC suspends the new Band A tariff.",
    )
    ensure_policy_situations(session, {IKEJA})
    assert assess(session, situation(session)).evidence_state != "disputed"


def test_wording_is_read_from_the_passage_not_the_model_text(
    session: Session, places: dict[str, int], nerc: Source, outlet: Source
) -> None:
    _two_orders(session, nerc)
    news = report(session, outlet, claim_text="Nothing was said about the tariff here.")
    news.text = "Customers are now being charged the new Band A rate."
    session.flush()
    ensure_policy_situations(session, {IKEJA})
    assert assess(session, situation(session)).evidence_state == "reported"
