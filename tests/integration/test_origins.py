"""Reporting-origin clustering (AS-025): syndicated copies are one origin, official documents are
their own, and the independence count counts origins, not documents."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal.evidence.origins import (
    SIMHASH_MAX_DISTANCE,
    assign_origin,
    independent_origin_count,
)
from africasignal.evidence.simhash import hamming_distance, simhash
from africasignal.jobs.handlers import process_document as process_document_module
from africasignal.models import Claim, EvidenceDocument, ReportingOrigin, Source
from africasignal.sources.base import ADAPTERS, ProcessResult

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)

WIRE_STORY = (
    "The pump price of petrol has risen to N870 per litre in Lagos, according to dealers, as depot "
    "prices climbed for the third week running. Marketers said the increase followed a rise in the "
    "exchange rate and supply constraints at several depots across the country. Motorists queued "
    "at filling stations in Ikeja, Surulere and Yaba on Monday morning, and some stations "
    "rationed sales to twenty litres per vehicle. The Independent Petroleum Marketers Association "
    "said it expected prices to stay high until supply from the Dangote refinery improved. "
    "Officials of the Nigerian Midstream and Downstream Petroleum Regulatory Authority said they "
    "were monitoring depot prices and would meet marketers later in the week to discuss supply. "
    "Commuters in Lagos said transport fares had already gone up by between ten and fifteen "
    "percent, and traders at Mile 12 market warned that food prices would follow the same path "
    "because of higher haulage costs. The Nigeria Labour Congress called on the government to "
    "explain the increase and to publish the depot price data it uses to set its benchmarks. "
    "Analysts at several Lagos brokerages said the naira, which has weakened for six sessions, "
    "remains the main driver of import costs, and that any relief at the pump would depend on "
    "stability in the foreign exchange market over the coming month."
)
OTHER_STORY = (
    "Electricity distribution companies in Nigeria announced a new tariff order for Band A "
    "customers effective next month, the regulator said in a statement issued in Abuja on Friday. "
    "The Nigerian Electricity Regulatory Commission said the order follows a review of generation "
    "costs and the exchange rate, and that customers in Bands B to E are not affected. Distribution "
    "companies must publish the new rates on their websites and notify customers in writing before "
    "the effective date, the statement said, and complaints should be sent to the commission's "
    "consumer affairs department or to the company's customer care lines within thirty days."
)


def _source(session: Session, slug: str, kind: str = "news_outlet") -> Source:
    source = Source(slug=slug, name=slug.title(), kind=kind, adapter="rss", schedule_minutes=30)
    session.add(source)
    session.flush()
    return source


_counter = 0


def _doc(
    session: Session,
    source: Source,
    text: str | None,
    *,
    url: str | None = None,
    when: datetime = NOW,
    sha: str | None = None,
) -> EvidenceDocument:
    global _counter
    _counter += 1
    url = url or f"https://{source.slug}.example/story-{_counter}"
    doc = EvidenceDocument(
        source_id=source.id,
        url=url,
        canonical_url=url,
        retrieved_at=when,
        published_at=when,
        content_sha256=sha or f"{_counter:064x}",
        storage_key=f"evidence/{_counter}",
        mime="text/html",
        simhash=simhash(text) if text else None,
    )
    session.add(doc)
    session.flush()
    return doc


def _claim(session: Session, doc: EvidenceDocument) -> Claim:
    claim = Claim(
        evidence_document_id=doc.id,
        claim_type="price_statement",
        text="Petrol rose.",
        passage="The pump price of petrol has risen",
        item_code="pms_litre",
        direction="up",
        extractor_version="test",
        valid=True,
    )
    session.add(claim)
    session.flush()
    return claim


def _origins(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(ReportingOrigin)) or 0


@pytest.fixture
def punch(session: Session) -> Source:
    return _source(session, "punch")


@pytest.fixture
def vanguard(session: Session) -> Source:
    return _source(session, "vanguard")


def test_fixture_copy_is_within_the_threshold_and_other_text_is_not() -> None:
    copy = WIRE_STORY + " (Reuters)"
    a, b, c = simhash(WIRE_STORY), simhash(copy), simhash(OTHER_STORY)
    assert a is not None and b is not None and c is not None
    assert hamming_distance(a, b) <= SIMHASH_MAX_DISTANCE
    assert hamming_distance(a, c) > SIMHASH_MAX_DISTANCE


def test_two_outlets_with_the_same_wire_text_are_one_origin(
    session: Session, punch: Source, vanguard: Source
) -> None:
    first = _doc(session, punch, WIRE_STORY)
    copy = _doc(session, vanguard, WIRE_STORY + " (Reuters)")

    origin = assign_origin(session, first)
    assert origin.kind == "outlet_report"
    assert assign_origin(session, copy).id == origin.id
    assert _origins(session) == 1
    assert origin.kind == "wire_report"  # two outlets, one text
    assert independent_origin_count(session, [_claim(session, first), _claim(session, copy)]) == 1


def test_a_different_story_is_a_new_origin(
    session: Session, punch: Source, vanguard: Source
) -> None:
    a = _doc(session, punch, WIRE_STORY)
    b = _doc(session, vanguard, OTHER_STORY)
    assert assign_origin(session, a).id != assign_origin(session, b).id
    assert independent_origin_count(session, [_claim(session, a), _claim(session, b)]) == 2


def test_the_same_url_captured_again_keeps_its_origin(session: Session, punch: Source) -> None:
    url = "https://punch.example/petrol"
    first = _doc(session, punch, WIRE_STORY, url=url)
    edited = _doc(session, punch, OTHER_STORY, url=url, when=NOW + timedelta(days=30))
    assert assign_origin(session, first).id == assign_origin(session, edited).id
    assert _origins(session) == 1


def test_copies_more_than_two_weeks_apart_are_separate_origins(
    session: Session, punch: Source, vanguard: Source
) -> None:
    first = _doc(session, punch, WIRE_STORY, when=NOW)
    recycled = _doc(session, vanguard, WIRE_STORY, when=NOW + timedelta(days=15))
    assert assign_origin(session, first).id != assign_origin(session, recycled).id

    inside = _doc(session, vanguard, WIRE_STORY, when=NOW + timedelta(days=13))
    assert assign_origin(session, inside).id == first.origin_id


def test_a_copy_captured_before_the_original_still_clusters(
    session: Session, punch: Source, vanguard: Source
) -> None:
    later = _doc(session, punch, WIRE_STORY, when=NOW)
    earlier = _doc(session, vanguard, WIRE_STORY, when=NOW - timedelta(days=5))
    assert assign_origin(session, later).id == assign_origin(session, earlier).id


def test_the_closest_match_wins(session: Session, punch: Source, vanguard: Source) -> None:
    guardian = _source(session, "guardian")
    a = _doc(session, punch, WIRE_STORY + " Copyright Punch.")  # 1 bit from the story
    b = _doc(session, vanguard, WIRE_STORY + " Read also: fuel queues")  # 3 bits, 4 from a
    assert assign_origin(session, a).id != assign_origin(session, b).id

    new = _doc(session, guardian, WIRE_STORY)
    assert assign_origin(session, new).id == a.origin_id


def test_official_documents_are_each_their_own_origin(session: Session) -> None:
    nbs = _source(session, "nbs", kind="official_statistics")
    nerc = _source(session, "nerc", kind="regulator")
    a = _doc(session, nbs, OTHER_STORY)
    b = _doc(session, nbs, OTHER_STORY)  # identical text, different document
    c = _doc(session, nerc, OTHER_STORY)
    origins = [assign_origin(session, d) for d in (a, b, c)]
    assert len({o.id for o in origins}) == 3
    assert [o.kind for o in origins] == ["official_dataset", "official_dataset", "primary_document"]


def test_a_news_copy_of_an_official_text_is_not_folded_into_the_official_origin(
    session: Session, punch: Source
) -> None:
    nerc = _source(session, "nerc", kind="regulator")
    order = _doc(session, nerc, OTHER_STORY)
    report = _doc(session, punch, OTHER_STORY)
    assert assign_origin(session, order).id != assign_origin(session, report).id


def test_assigning_twice_changes_nothing(session: Session, punch: Source) -> None:
    doc = _doc(session, punch, WIRE_STORY)
    first = assign_origin(session, doc)
    assert assign_origin(session, doc).id == first.id
    assert _origins(session) == 1


def test_a_document_without_text_gets_its_own_origin(session: Session, punch: Source) -> None:
    a = _doc(session, punch, None)
    b = _doc(session, punch, None)
    assert assign_origin(session, a).id != assign_origin(session, b).id


def test_origin_label_names_the_source_and_date(session: Session, punch: Source) -> None:
    origin = assign_origin(session, _doc(session, punch, WIRE_STORY))
    assert origin.label == "Punch report, 20 Sep 2026"
    assert origin.first_seen_at == NOW


def test_independence_count_ignores_unclustered_and_withdrawn_documents(
    session: Session, punch: Source, vanguard: Source
) -> None:
    a = _doc(session, punch, WIRE_STORY)
    b = _doc(session, vanguard, OTHER_STORY)
    unclustered = _doc(session, vanguard, "Some other story about food prices in Kano markets.")
    assign_origin(session, a)
    assign_origin(session, b)
    claims = [_claim(session, a), _claim(session, b), _claim(session, unclustered)]
    assert independent_origin_count(session, claims) == 2

    b.status = "withdrawn"
    session.flush()
    assert independent_origin_count(session, claims) == 1
    assert independent_origin_count(session, []) == 0


def test_two_claims_on_one_document_are_one_origin(session: Session, punch: Source) -> None:
    doc = _doc(session, punch, WIRE_STORY)
    assign_origin(session, doc)
    assert independent_origin_count(session, [_claim(session, doc), _claim(session, doc)]) == 1


def test_process_document_assigns_the_origin_before_the_adapter_runs(
    session: Session, punch: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = _doc(session, punch, WIRE_STORY)
    seen: list[int | None] = []

    class Adapter:
        slug_prefix = "test"

        def process(self, document: EvidenceDocument, ctx: Any) -> ProcessResult:
            seen.append(document.origin_id)
            return ProcessResult()

    monkeypatch.setitem(ADAPTERS, "rss", Adapter())
    monkeypatch.setattr(process_document_module, "capture", lambda *a, **k: doc)
    monkeypatch.setattr(process_document_module, "get_store", lambda: None)
    ctx = SimpleNamespace(
        session=session,
        job=SimpleNamespace(id=1, payload={"source_id": punch.id, "url": doc.url}),
    )
    process_document_module.process_document(ctx)  # type: ignore[arg-type]

    assert seen == [doc.origin_id] and doc.origin_id is not None
