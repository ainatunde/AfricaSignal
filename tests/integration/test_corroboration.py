"""Corroboration, disputes and possible factors for T1 from real claims (AS-027), on PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.evidence.origins import assign_origin
from africasignal.evidence.simhash import simhash
from africasignal.jobs.handlers import process_document as process_document_module
from africasignal.jobs.handlers.resolve_places import resolve_places_job
from africasignal.models import (
    AssessmentInput,
    AssessmentVersion,
    Claim,
    EvidenceDocument,
    Job,
    Measurement,
    Place,
    Situation,
    Source,
    SourcePermission,
)
from africasignal.publish.claim_assessments import request_claim_assessments
from africasignal.publish.invalidation import plan_corrections
from africasignal.publish.situations import assess_situation, ensure_situations, slug_for
from africasignal.publish.versions import apply_policy, release_held
from africasignal.sources.base import ADAPTERS, ProcessResult
from africasignal.storage import S3Store
from tests.integration.nbs_support import add_places, add_source, import_bytes, make_store
from tests.integration.outlet_support import approved_source
from tests.integration.test_origins import WIRE_STORY, _doc

PMS_SEP, PMS_OCT = "FUEL_SEPT_2024_REPORT.xlsx", "PMS_OCT_2024_REPORT.xlsx"
WHEN = datetime(2024, 11, 25, 12, tzinfo=UTC)  # a week after the October 2024 petrol release
NOW = WHEN  # sources are vetted relative to this clock
PUBLISHED = datetime(2024, 11, 12, 9, tzinfo=UTC)


def vetted(session: Session, slug: str, **kwargs: Any) -> Source:
    """A source approved long enough before the assessment clock to be trusted."""
    return approved_source(session, slug, now=WHEN, **kwargs)


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from make_store()


@pytest.fixture
def places(session: Session) -> dict[str, int]:
    return add_places(session)


@pytest.fixture
def lagos(session: Session, store: S3Store, places: dict[str, int]) -> Situation:
    """The Lagos petrol situation, from real NBS September and October 2024 files (up 8.0 %)."""
    nbs = add_source(session)
    import_bytes(session, store, nbs, PMS_SEP)
    import_bytes(session, store, nbs, PMS_OCT)
    ensure_situations(session, {("pms_litre", pid) for pid in places.values()})
    return _situation(session, "NG-LA")


@pytest.fixture
def outlet(session: Session) -> Source:
    return vetted(session, "punch")


def _situation(session: Session, code: str) -> Situation:
    return session.scalars(
        select(Situation).where(Situation.slug == slug_for("pms_litre", code))
    ).one()


def news(
    session: Session,
    source: Source,
    place_id: int | None,
    *,
    text: str = WIRE_STORY,
    direction: str = "up",
    item_code: str | None = "pms_litre",
    valid: bool = True,
    claim_type: str = "price_statement",
    claim_text: str = "The pump price of petrol has risen.",
    doc: EvidenceDocument | None = None,
    **fields: Any,
) -> Claim:
    doc = doc or _doc(session, source, text, when=PUBLISHED)
    assign_origin(session, doc)
    values: dict[str, Any] = {
        "occurred_from": date(2024, 10, 1),
        "occurred_to": date(2024, 10, 31),
        "time_precision": "month",
        "place_precision": "state",
    } | fields
    claim = Claim(
        evidence_document_id=doc.id,
        claim_type=claim_type,
        text=claim_text,
        passage=claim_text.rstrip("."),
        item_code=item_code,
        direction=direction,
        place_id=place_id,
        extractor_version="test",
        valid=valid,
        **values,
    )
    session.add(claim)
    session.flush()
    return claim


def assess(session: Session, situation: Situation) -> AssessmentVersion:
    outcome = assess_situation(session, situation.id, WHEN)
    assert outcome.version is not None
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


# --- corroboration -----------------------------------------------------------------------------


def test_without_claims_the_official_figure_is_reported(session: Session, lagos: Situation) -> None:
    assert assess(session, lagos).evidence_state == "reported"


def test_a_news_claim_for_the_state_and_month_corroborates(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    claim = news(session, outlet, places["NG-LA"])
    version = assess(session, lagos)
    assert version.evidence_state == "corroborated"
    assert version.headline.endswith("rose 8.0% in October 2024 to ₦1,080.95 (NBS)")
    assert inputs_of(session, version, "claim") == {claim.id}
    assert claim.evidence_document_id in inputs_of(session, version, "evidence_document")
    assert "No independent report for this state and month" not in version.unknowns
    fact = next(f for f in version.facts if f["label"] == "Independent reports")
    assert (fact["value"], fact["place_code"]) == (1, "NG-LA")


def test_a_claim_about_a_place_inside_the_state_counts(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    ikeja = Place(kind="lga", name="Ikeja", code="NG-LA-IKEJA", parent_id=places["NG-LA"])
    session.add(ikeja)
    session.flush()
    news(session, outlet, ikeja.id)
    assert assess(session, lagos).evidence_state == "corroborated"


def test_claims_about_other_places_items_or_types_do_not_count(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    news(session, outlet, places["NG"], text=WIRE_STORY + " one")  # national, not Lagos
    news(session, outlet, places["NG-OG"], text=WIRE_STORY + " two")  # another state
    news(session, outlet, places["NG-LA"], item_code="ago_litre", text=WIRE_STORY + " three")
    news(session, outlet, places["NG-LA"], item_code=None, text=WIRE_STORY + " four")
    news(session, outlet, places["NG-LA"], claim_type="policy_statement", text=WIRE_STORY + " five")
    news(session, outlet, None, text=WIRE_STORY + " six")  # no place resolved
    assert assess(session, lagos).evidence_state == "reported"


def test_a_national_claim_corroborates_the_national_situation_only(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    news(session, outlet, places["NG"])
    assert assess(session, _situation(session, "NG")).evidence_state == "corroborated"
    assert assess(session, lagos).evidence_state == "reported"


def test_invalid_claims_and_withdrawn_documents_do_not_count(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    news(session, outlet, places["NG-LA"], valid=False)
    gone = news(session, outlet, places["NG-LA"], text=WIRE_STORY + " another")
    session.get(EvidenceDocument, gone.evidence_document_id).status = "withdrawn"  # type: ignore[union-attr]
    session.flush()
    assert assess(session, lagos).evidence_state == "reported"


def test_syndicated_copies_are_one_origin(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    sources = [vetted(session, slug) for slug in ("punch", "vanguard", "thisday")]
    for source in sources:
        news(session, source, places["NG-LA"], text=WIRE_STORY + " (Reuters)")
    version = assess(session, lagos)
    assert version.evidence_state == "corroborated"
    fact = next(f for f in version.facts if f["label"] == "Independent reports")
    assert fact["value"] == 1 and len(fact["evidence_ids"]) == 3


def test_a_reprint_of_the_official_document_never_corroborates(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    official_doc = session.get(
        EvidenceDocument,
        session.scalars(
            select(Measurement.evidence_document_id).where(Measurement.place_id == places["NG-LA"])
        ).first(),
    )
    assert official_doc is not None
    official_doc.simhash = simhash(WIRE_STORY)  # the NBS text, as far as similarity goes
    session.flush()
    news(session, outlet, places["NG-LA"], text=WIRE_STORY + " (NBS)")
    assert assess(session, lagos).evidence_state == "reported"


def test_a_news_claim_that_is_a_month_stale_does_not_corroborate(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    news(
        session,
        outlet,
        places["NG-LA"],
        occurred_from=date(2024, 7, 1),
        occurred_to=date(2024, 7, 31),
    )
    assert assess(session, lagos).evidence_state == "reported"


# --- disputed ----------------------------------------------------------------------------------


def test_a_regulator_saying_the_opposite_disputes(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    nmdpra = vetted(session, "nmdpra", kind="regulator")
    claim = news(session, nmdpra, places["NG-LA"], direction="down", text="Prices fell in Lagos.")
    version = assess(session, lagos)
    assert version.evidence_state == "disputed"
    assert version.severity == "high"  # the figures and their severity are untouched
    assert inputs_of(session, version, "claim") == {claim.id}


# --- possible factors --------------------------------------------------------------------------


def test_a_claim_that_names_a_factor_supports_it(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    claim = news(
        session,
        outlet,
        places["NG-LA"],
        claim_text="Dealers said the exchange rate pushed up the pump price.",
    )
    version = assess(session, lagos)
    by_code = {f["code"]: f for f in version.possible_factors}
    assert by_code["exchange_rate"]["status"] == "supported"
    assert by_code["exchange_rate"]["evidence_ids"] == [claim.evidence_document_id]
    assert by_code["crude_price"]["status"] == "not_checked"
    assert by_code["supply_disruption"]["status"] == "not_checked"


# --- versions and corrections ------------------------------------------------------------------


def test_a_new_claim_makes_a_new_version_and_the_same_inputs_do_not(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    first = assess(session, lagos)
    assert assess_situation(session, lagos.id, WHEN).outcome == "unchanged"
    news(session, outlet, places["NG-LA"])
    second = assess(session, lagos)
    assert (first.version, second.version) == (1, 2)
    assert (first.evidence_state, second.evidence_state) == ("reported", "corroborated")
    assert second.change_summary == "Re-assessed after the inputs changed"


def test_an_invalidated_claim_corrects_the_corroborated_version(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    claim = news(session, outlet, places["NG-LA"])
    version = assess(session, lagos)
    apply_policy(session, version.id, WHEN)  # high severity and a first version: held (R7) ...
    release_held(session, WHEN + timedelta(minutes=61))  # ... then released
    assert version.status == "published" and version.evidence_state == "corroborated"

    claim.valid = False  # an operator marks it invalid
    session.flush()
    plan = plan_corrections(session, "claim", [claim.id])
    assert plan == {lagos.id: "Corrected: a report behind this assessment was found to be invalid"}
    corrected = assess_situation(session, lagos.id, WHEN, correction=plan[lagos.id]).version
    assert corrected is not None and corrected.evidence_state == "reported"
    apply_policy(session, corrected.id, WHEN, kind="correction")
    assert version.status == "superseded" and lagos.current_version_id == corrected.id


# --- queueing assessments when claims arrive ---------------------------------------------------


def _jobs(session: Session) -> list[Job]:
    return list(session.scalars(select(Job).where(Job.kind == "assess_situation").order_by(Job.id)))


def test_claims_queue_an_assessment_of_the_state_situation_only(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    claim = news(session, outlet, places["NG-LA"])
    job_ids = request_claim_assessments(session, claim.evidence_document_id)
    assert [j.payload["situation_id"] for j in _jobs(session)] == [lagos.id]
    assert len(job_ids) == 1
    assert request_claim_assessments(session, claim.evidence_document_id) == []  # deduplicated


def test_a_claim_in_an_lga_queues_the_state_situation(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    ikeja = Place(kind="lga", name="Ikeja", code="NG-LA-IKEJA", parent_id=places["NG-LA"])
    session.add(ikeja)
    session.flush()
    claim = news(session, outlet, ikeja.id)
    request_claim_assessments(session, claim.evidence_document_id)
    assert [j.payload["situation_id"] for j in _jobs(session)] == [lagos.id]


def test_later_claims_on_the_same_document_queue_again(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    first = news(session, outlet, places["NG-LA"])
    request_claim_assessments(session, first.evidence_document_id)
    news(
        session,
        outlet,
        places["NG-LA"],
        doc=session.get(EvidenceDocument, first.evidence_document_id),
    )
    assert len(request_claim_assessments(session, first.evidence_document_id)) == 1
    assert len(_jobs(session)) == 2


def test_invalid_claims_and_claims_without_a_situation_queue_nothing(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    bad = news(session, outlet, places["NG-LA"], valid=False, text=WIRE_STORY + " x")
    elsewhere = news(session, outlet, places["NG-OG"], text=WIRE_STORY + " y")
    session.execute(  # no Ogun situation exists
        Situation.__table__.delete().where(Situation.place_id == places["NG-OG"])
    )
    assert request_claim_assessments(session, bad.evidence_document_id) == []
    assert request_claim_assessments(session, elsewhere.evidence_document_id) == []


def test_resolving_places_queues_the_assessment(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int]
) -> None:
    claim = news(session, outlet, None, place_candidates=["Lagos"])
    job = SimpleNamespace(id=1, payload={"document_id": claim.evidence_document_id})
    resolve_places_job(SimpleNamespace(session=session, job=job))  # type: ignore[arg-type]
    assert claim.place_id == places["NG-LA"]
    assert [j.payload["situation_id"] for j in _jobs(session)] == [lagos.id]


def test_process_document_queues_assessments_for_claims_an_adapter_made(
    session: Session, lagos: Situation, outlet: Source, places: dict[str, int], monkeypatch: Any
) -> None:
    claim = news(session, outlet, places["NG-LA"])
    doc = session.get(EvidenceDocument, claim.evidence_document_id)

    class Adapter:
        def process(self, document: EvidenceDocument, ctx: Any) -> ProcessResult:
            return ProcessResult(claims=1)

    monkeypatch.setitem(ADAPTERS, "rss", Adapter())
    monkeypatch.setattr(process_document_module, "capture", lambda *a, **k: doc)
    monkeypatch.setattr(process_document_module, "get_store", lambda: None)
    ctx = SimpleNamespace(
        session=session,
        job=SimpleNamespace(id=1, payload={"source_id": outlet.id, "url": doc.url}),  # type: ignore[union-attr]
    )
    process_document_module.process_document(ctx)  # type: ignore[arg-type]
    assert [j.payload["situation_id"] for j in _jobs(session)] == [lagos.id]


# --- look-alike sources (security review S-08) -------------------------------------------------


def _count(version: AssessmentVersion) -> int:
    fact = next(f for f in version.facts if f["label"] == "Independent reports")
    return int(fact["value"])


def _wire(n: int) -> str:
    return f"{WIRE_STORY} Variant {n}: " + " ".join(f"w{n}x{i}" for i in range(80))


def test_a_swarm_of_unapproved_sites_does_not_corroborate(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    for i in range(12):
        fake = vetted(session, f"fake{i}", approved_days_ago=None)  # nobody approved it
        news(session, fake, places["NG-LA"], text=_wire(i))
    version = assess(session, lagos)
    assert version.evidence_state == "reported"
    assert inputs_of(session, version, "claim") == set()


def test_sites_approved_only_just_now_do_not_corroborate_until_they_have_aged(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    for i in range(5):
        news(
            session,
            vetted(session, f"new{i}", approved_days_ago=29),
            places["NG-LA"],
            text=_wire(i),
        )
    assert assess(session, lagos).evidence_state == "reported"
    news(session, vetted(session, "aged", approved_days_ago=30), places["NG-LA"], text=_wire(99))
    assert assess(session, lagos).evidence_state == "corroborated"


def test_the_age_counts_from_the_first_approval_not_the_latest(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    source = vetted(session, "veteran", approved_days_ago=200)
    session.add(  # re-approved yesterday after a terms review
        SourcePermission(
            source_id=source.id,
            version=2,
            may_collect=True,
            may_store_full_text=False,
            may_republish_numbers=True,
            approved_at=WHEN - timedelta(days=1),
        )
    )
    session.flush()
    news(session, source, places["NG-LA"])
    assert assess(session, lagos).evidence_state == "corroborated"


def test_a_source_whose_permission_was_withdrawn_or_that_is_inactive_stops_counting(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    source = vetted(session, "punch")
    news(session, source, places["NG-LA"])
    assert assess(session, lagos).evidence_state == "corroborated"

    session.add(
        SourcePermission(
            source_id=source.id,
            version=2,
            may_collect=False,
            may_store_full_text=False,
            may_republish_numbers=False,
            approved_at=WHEN,
        )
    )
    session.flush()
    assert assess(session, lagos).evidence_state == "reported"

    source.active = False
    session.flush()
    assert assess(session, lagos).evidence_state == "reported"


def test_a_domain_gdelt_merely_pointed_to_does_not_corroborate(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    gdelt = vetted(session, "gdelt", kind="aggregator", adapter="gdelt")
    news(session, gdelt, places["NG-LA"])
    assert assess(session, lagos).evidence_state == "reported"


def test_approved_outlets_with_one_owner_count_once(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    for i in range(6):
        outlet_ = vetted(session, f"brand{i}", owner="Shell Media Nigeria Ltd")
        news(session, outlet_, places["NG-LA"], text=_wire(i))
    version = assess(session, lagos)
    assert version.evidence_state == "corroborated" and _count(version) == 1


def test_approved_outlets_on_one_registered_domain_count_once(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    for i in range(6):
        doc = _doc(
            session,
            vetted(session, f"section{i}"),
            _wire(i),
            url=f"https://section{i}.samesite.example/story",
            when=PUBLISHED,
        )
        news(session, vetted(session, f"x{i}"), places["NG-LA"], doc=doc)
    version = assess(session, lagos)
    assert version.evidence_state == "corroborated" and _count(version) == 1


def test_outlets_sharing_a_byline_count_once_but_generic_bylines_do_not_link(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    for i in range(4):
        doc = _doc(session, vetted(session, f"bl{i}"), _wire(i), when=PUBLISHED)
        doc.byline = "By Jane Doe" if i < 3 else "Staff"
        news(session, vetted(session, f"c{i}"), places["NG-LA"], doc=doc)
    assert _count(assess(session, lagos)) == 2  # Jane Doe's three copies, and one more


def test_genuinely_separate_approved_outlets_count_separately_and_are_named(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    for slug, owner in (("punch", "Punch Nigeria Ltd"), ("vanguard", "Vanguard Media Ltd")):
        news(session, vetted(session, slug, owner=owner), places["NG-LA"], text=_wire(len(slug)))
    version = assess(session, lagos)
    fact = next(f for f in version.facts if f["label"] == "Independent reports")
    assert fact["value"] == 2 and fact["outlets"] == ["Punch", "Vanguard"]
    assert fact["source_label"] == "Punch, Vanguard"


def test_an_unapproved_site_cannot_support_a_factor_or_plant_one_in_its_summary(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    fake = vetted(session, "fake", approved_days_ago=None)
    news(session, fake, places["NG-LA"], claim_text="The exchange rate pushed up the pump price.")
    assert {f["status"] for f in assess(session, lagos).possible_factors} == {"not_checked"}

    real = vetted(session, "real")
    claim = news(session, real, places["NG-LA"], text=WIRE_STORY + " real")
    claim.text = "Crude oil and the exchange rate explain it."  # the model's summary, not the page
    session.flush()
    assert {f["status"] for f in assess(session, lagos).possible_factors} == {"not_checked"}


def test_an_unapproved_regulator_look_alike_cannot_dispute(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    fake = vetted(session, "nmdpra-official", kind="regulator", approved_days_ago=None)
    news(session, fake, places["NG-LA"], direction="down", text="Prices fell in Lagos.")
    assert assess(session, lagos).evidence_state == "reported"


def test_trust_is_judged_on_the_clock_of_the_assessment(
    session: Session, lagos: Situation, places: dict[str, int]
) -> None:
    news(session, vetted(session, "punch", approved_days_ago=40), places["NG-LA"])
    early = assess_situation(session, lagos.id, WHEN - timedelta(days=20)).version
    assert early is not None and early.evidence_state == "reported"  # approved only 20 days before
    late = assess_situation(session, lagos.id, WHEN).version
    assert late is not None and late.evidence_state == "corroborated"
