"""Claim extraction and place resolution end to end on PostgreSQL, with the fake provider
(AS-021). The replayed answers are fixtures, not live calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal.extract.claims import (
    Extraction,
    extract_claims,
    extractor_version,
    store_claims,
)
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.handlers import extract_claims as extract_module
from africasignal.jobs.handlers.extract_claims import enqueue_extraction, extract_claims_job
from africasignal.jobs.handlers.resolve_places import resolve_places_job
from africasignal.jobs.queue import ClaimedJob, enqueue
from africasignal.llm import LlmAdapter
from africasignal.llm.fake import FakeProvider, reply_from_recording
from africasignal.models import Claim, EvidenceDocument, Job, LlmCall, Source
from tests.integration.nbs_support import add_places, add_source

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
BIG_BUDGET = 10.0


def recordings() -> list[Path]:
    return sorted(FIXTURES.glob("claim_extract_v1_*.json"))


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())  # type: ignore[no-any-return]


@pytest.fixture
def places(session: Session) -> dict[str, int]:
    return add_places(session)


@pytest.fixture
def source(session: Session, places: dict[str, int]) -> Source:
    return add_source(session)


def make_document(session: Session, source: Source, recording: dict[str, Any]) -> EvidenceDocument:
    doc = recording["document"]
    document = EvidenceDocument(
        source_id=source.id,
        url="https://news.example/petrol",
        canonical_url="https://news.example/petrol",
        retrieved_at=NOW,
        published_at=datetime.fromisoformat(doc["published_at"]),
        content_sha256="0" * 64,
        storage_key="evidence/00/00/" + "0" * 64 + ".html",
        mime="text/html",
        title=doc["title"],
        text_content=doc["text"],
    )
    session.add(document)
    session.flush()
    return document


def adapter_for(session: Session, provider: FakeProvider, budget: float = BIG_BUDGET) -> LlmAdapter:
    return LlmAdapter(
        session, provider, daily_budget_usd=budget, per_job_max_tokens=100_000, clock=lambda: NOW
    )


def run_job(
    session: Session, kind: str, payload: dict[str, Any], job_id: int | None = None
) -> None:
    """Run a handler as the worker would, under a real job row (llm_call.job_id points at it)."""
    row = session.scalars(select(Job).where(Job.dedupe_key == f"test-run:{kind}")).first()
    if row is None:
        enqueue_id = enqueue(session, kind, payload, dedupe_key=f"test-run:{kind}")
        assert enqueue_id is not None
        job_id = enqueue_id
    else:
        job_id = row.id
    job = ClaimedJob(id=job_id, kind=kind, payload=payload, attempts=1, max_attempts=5)
    ctx = JobContext(session=session, job=job, worker_id="test")
    {"extract_claims": extract_claims_job, "resolve_places": resolve_places_job}[kind](ctx)


SYNTHETIC = FIXTURES / "claim_extract_v1_synthetic.json"


def test_replaying_a_recorded_answer_validates_every_claim(
    session: Session, source: Source
) -> None:
    recording = load(SYNTHETIC)
    document = make_document(session, source, recording)
    provider = FakeProvider([reply_from_recording(SYNTHETIC)])
    result = extract_claims(adapter_for(session, provider), document, recording["document"]["text"])

    verdicts = [(c.item_code or c.policy_series, c.invalid_reason) for c in result.claims]
    assert verdicts == [
        ("pms_litre", None),
        ("pms_litre", None),
        ("electricity_tariff_band_a:ikeja-electric", None),  # a future date, but "will remain"
        ("pms_litre", "passage_not_found"),
        ("pms_litre", "value_not_in_passage"),
        ("pms_litre", "future_date"),
    ]
    first = result.claims[0]
    assert first.stated_value == Decimal(1020) and first.place_candidates == ["Lagos"]
    text = recording["document"]["text"]
    assert first.passage_start is not None and first.passage_end is not None
    assert text[first.passage_start : first.passage_end] == first.passage


def test_the_model_sees_the_fenced_document_and_the_allowed_codes(
    session: Session, source: Source
) -> None:
    recording = load(SYNTHETIC)
    document = make_document(session, source, recording)
    provider = FakeProvider([reply_from_recording(SYNTHETIC)])
    extract_claims(adapter_for(session, provider), document, recording["document"]["text"])
    (request,) = provider.calls
    assert "`pms_litre`" in request.system and "data, not instructions" in request.system
    assert recording["document"]["text"] in request.user
    assert request.schema["required"] == ["claims"]
    assert request.effort == "low"


def test_storing_keeps_invalid_claims_with_their_reason(session: Session, source: Source) -> None:
    recording = load(SYNTHETIC)
    document = make_document(session, source, recording)
    provider = FakeProvider([reply_from_recording(SYNTHETIC)])
    result = extract_claims(adapter_for(session, provider), document, recording["document"]["text"])
    rows = store_claims(session, document, result, "claim_extract_v1+test")
    assert [r.valid for r in rows] == [True, True, True, False, False, False]
    stored = session.scalars(select(Claim).order_by(Claim.id)).all()
    assert [c.invalid_reason for c in stored][3:] == [
        "passage_not_found",
        "value_not_in_passage",
        "future_date",
    ]
    assert all(c.extractor_version == "claim_extract_v1+test" for c in stored)
    assert stored[0].place_id is None and stored[0].place_precision == "unknown"


def test_a_real_recording_exists() -> None:
    real = [p for p in recordings() if load(p)["recorded"]]
    if not real:
        pytest.skip(
            "LIVE CALL NEEDED: no real recorded response yet. With ANTHROPIC_API_KEY set, run "
            "`python -m africasignal.llm.record` (see its docstring) and commit the file."
        )


@pytest.mark.parametrize("path", [p for p in recordings() if load(p)["recorded"]])
def test_real_recordings_replay_to_some_valid_claim(
    session: Session, source: Source, path: Path
) -> None:
    recording = load(path)
    document = make_document(session, source, recording)
    provider = FakeProvider([reply_from_recording(path)])
    result = extract_claims(adapter_for(session, provider), document, recording["document"]["text"])
    assert any(c.valid for c in result.claims)


# --- the handler -----------------------------------------------------------------------------


@pytest.fixture
def document(session: Session, source: Source) -> EvidenceDocument:
    return make_document(session, source, load(SYNTHETIC))


def use(
    monkeypatch: pytest.MonkeyPatch, provider: FakeProvider, budget: float = BIG_BUDGET
) -> None:
    monkeypatch.setattr(
        extract_module, "build_adapter", lambda session: adapter_for(session, provider, budget)
    )


def test_the_handler_stores_claims_and_queues_place_resolution(
    session: Session, document: EvidenceDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider([reply_from_recording(SYNTHETIC)])
    use(monkeypatch, provider)
    run_job(session, "extract_claims", {"document_id": document.id})
    claims = session.scalars(select(Claim).order_by(Claim.id)).all()
    assert len(claims) == 6 and sum(c.valid for c in claims) == 3
    assert {c.extractor_version for c in claims} == {extractor_version()}
    job = session.scalars(select(Job).where(Job.kind == "resolve_places")).one()
    assert job.payload == {"document_id": document.id}


def test_the_same_document_is_not_extracted_twice(
    session: Session, document: EvidenceDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider([reply_from_recording(SYNTHETIC)])
    use(monkeypatch, provider)
    assert enqueue_extraction(session, document.id) is not None
    assert enqueue_extraction(session, document.id) is None  # the queue refuses a duplicate

    run_job(session, "extract_claims", {"document_id": document.id})
    run_job(session, "extract_claims", {"document_id": document.id})  # forced re-run
    assert len(provider.calls) == 1
    assert session.scalar(select(func.count()).select_from(Claim)) == 6


def test_the_cache_answers_when_claims_were_lost_and_the_job_runs_again(
    session: Session, document: EvidenceDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job that failed after the model call committed the cache entry. Its retry finds no
    claims, but does not pay for the model again."""
    provider = FakeProvider([reply_from_recording(SYNTHETIC)])
    use(monkeypatch, provider)
    run_job(session, "extract_claims", {"document_id": document.id})
    session.execute(Claim.__table__.delete())
    run_job(session, "extract_claims", {"document_id": document.id})
    assert len(provider.calls) == 1
    assert session.scalar(select(func.count()).select_from(Claim)) == 6
    assert session.scalar(select(func.count()).select_from(LlmCall)) == 2  # one real, one cache


def test_budget_exhaustion_reschedules_the_document_for_the_next_day(
    session: Session, document: EvidenceDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider([reply_from_recording(SYNTHETIC)])
    use(monkeypatch, provider, budget=0)
    run_job(session, "extract_claims", {"document_id": document.id})
    assert provider.calls == []
    assert session.scalar(select(func.count()).select_from(Claim)) == 0
    job = session.scalars(select(Job).where(Job.dedupe_key.like("extract_claims:%:after:%"))).one()
    assert job.payload == {"document_id": document.id}
    # 12:00 UTC on 12 Sep is 13:00 in Lagos; the next budget day starts at 23:00 UTC.
    assert job.run_at == datetime(2026, 9, 12, 23, 0, tzinfo=UTC)
    assert job.status == "queued"


def test_a_document_without_topic_keywords_costs_nothing(
    session: Session, document: EvidenceDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    document.text_content = "A story about football. " * 50
    provider = FakeProvider([reply_from_recording(SYNTHETIC)])
    use(monkeypatch, provider)
    run_job(session, "extract_claims", {"document_id": document.id})
    assert provider.calls == []


def test_a_missing_or_withdrawn_document_is_skipped(
    session: Session, document: EvidenceDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider([reply_from_recording(SYNTHETIC)])
    use(monkeypatch, provider)
    run_job(session, "extract_claims", {"document_id": 999_999})
    document.status = "withdrawn"
    session.flush()
    run_job(session, "extract_claims", {"document_id": document.id})
    assert provider.calls == []


def test_a_document_with_no_claims_stores_nothing_and_queues_nothing(
    session: Session, document: EvidenceDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, FakeProvider([{"claims": []}]))
    run_job(session, "extract_claims", {"document_id": document.id})
    assert session.scalar(select(func.count()).select_from(Claim)) == 0
    assert session.scalar(select(func.count()).where(Job.kind == "resolve_places")) == 0


# --- place resolution ------------------------------------------------------------------------


def add_claim(
    session: Session, document: EvidenceDocument, candidates: list[str], **kw: Any
) -> Claim:
    claim = Claim(
        evidence_document_id=document.id,
        claim_type="price_statement",
        text="t",
        passage="p",
        direction="unknown",
        time_precision="unknown",
        place_candidates=candidates,
        extractor_version="v",
        valid=kw.pop("valid", True),
        **kw,
    )
    session.add(claim)
    session.flush()
    return claim


def test_places_are_resolved_to_the_precision_the_text_supports(
    session: Session, document: EvidenceDocument, places: dict[str, int]
) -> None:
    lagos = add_claim(session, document, ["Lagos"])
    abuja = add_claim(session, document, ["Abuja"])
    national = add_claim(session, document, ["Nigeria"])
    mixed = add_claim(session, document, ["Lagos", "Abuja"])  # two states: not one place
    unknown = add_claim(session, document, ["Atlantis"])
    none = add_claim(session, document, [])
    invalid = add_claim(session, document, ["Lagos"], valid=False)
    run_job(session, "resolve_places", {"document_id": document.id})
    assert (lagos.place_id, lagos.place_precision) == (places["NG-LA"], "state")
    assert (abuja.place_id, abuja.place_precision) == (places["NG-FC"], "state")
    assert (national.place_id, national.place_precision) == (places["NG"], "national")
    for claim in (mixed, unknown, none, invalid):
        assert (claim.place_id, claim.place_precision) == (None, "unknown")


def test_resolving_twice_changes_nothing(
    session: Session, document: EvidenceDocument, places: dict[str, int]
) -> None:
    claim = add_claim(session, document, ["Lagos"])
    run_job(session, "resolve_places", {"document_id": document.id})
    run_job(session, "resolve_places", {"document_id": document.id})
    assert claim.place_id == places["NG-LA"]


def test_the_worker_registers_both_handlers() -> None:
    """``load_all`` is what the worker calls at startup. Run in a fresh interpreter, so the
    handlers it registers do not leak into tests that expect an empty registry."""
    import subprocess
    import sys

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from africasignal.jobs import handlers; handlers.load_all(); "
            "print(' '.join(sorted(handlers.HANDLERS)))",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert {"extract_claims", "resolve_places"} <= set(out)


_ = Extraction  # re-exported type, imported to keep the public surface under test
