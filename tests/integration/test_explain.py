"""Explanation text for real assessment versions on PostgreSQL, with the fake provider (AS-028).
The replayed answers are hand-written stand-ins, not live model calls."""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal.assess import explain as explain_module
from africasignal.assess.explain import (
    PROMPT_VERSION,
    build_input,
    explain_version,
    needs_explanation,
    validate_explanation,
    versions_missing_explanation,
)
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.handlers import assess_situation as assess_handler
from africasignal.jobs.handlers import explain_version as explain_jobs
from africasignal.jobs.queue import ClaimedJob, enqueue
from africasignal.llm import BudgetExhausted, LlmAdapter
from africasignal.llm.fake import FakeProvider
from africasignal.models import AssessmentVersion, Job, LlmCall, Place, Situation, Source
from africasignal.publish.factfmt import format_value
from africasignal.publish.situations import assess_situation, ensure_situations
from africasignal.publish.versions import apply_policy, set_publication_suspended
from africasignal.storage import S3Store
from tests.integration.nbs_support import add_places, add_source, import_bytes, make_store

GAS_OCT = "GAS_PRICE_WATCH_OCT_2024.xlsx"
PMS_OCT = "PMS_OCT_2024_REPORT.xlsx"
WHEN = datetime(2024, 11, 25, 12, tzinfo=UTC)  # a week after the October 2024 releases
TODAY = datetime(2026, 9, 30, 12, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from make_store()


@pytest.fixture
def source(session: Session) -> Source:
    add_places(session)
    return add_source(session)


def published(session: Session, store: S3Store, source: Source) -> AssessmentVersion:
    """The national 12.5 kg cooking gas assessment for October 2024: reported, medium, published."""
    import_bytes(session, store, source, GAS_OCT)
    place = session.scalars(select(Place.id).where(Place.code == "NG")).one()
    (situation,) = ensure_situations(session, [("lpg_12_5kg", place)])
    version = assess_situation(session, situation.id, WHEN).version
    assert version is not None
    decision = apply_policy(session, version.id, WHEN)
    assert decision is not None and decision.status == "published"
    return version


def good_text(version: AssessmentVersion) -> str:
    """A paragraph that passes the validator, written from the version's own facts."""
    by_label = {f["label"]: f for f in version.facts}
    current = by_label["Current price"]
    change = by_label["Month-on-month change"]
    return (
        f"NBS data shows 12.5kg cooking gas cost {format_value(current['value'], current['unit'])} "
        f"on average across Nigeria in October 2024, a change of {change['value']}% on the month. "
        "Only NBS reports this, so prices near you may differ."
    )


def adapter_for(session: Session, provider: FakeProvider, budget: float = 10.0) -> LlmAdapter:
    return LlmAdapter(
        session, provider, daily_budget_usd=budget, per_job_max_tokens=100_000, clock=lambda: TODAY
    )


def answer(text: str) -> dict[str, str]:
    return {"explanation": text}


def test_a_valid_explanation_is_stored_with_the_model_and_prompt_version(
    session: Session, store: S3Store, source: Source
) -> None:
    v = published(session, store, source)
    text = good_text(v)
    assert validate_explanation(text, build_input(session, v)) == []
    provider = FakeProvider([answer(text)])

    assert explain_version(session, adapter_for(session, provider), v.id) == "explained"

    assert v.explanation == text
    assert (v.prompt_version, v.model_id) == (PROMPT_VERSION, "claude-sonnet-5-5")
    assert v.status == "published" and v.headline.startswith("Average cooking gas")
    (request,) = provider.calls
    assert request.effort == "low" and request.schema["required"] == ["explanation"]
    assert "BEGIN INPUT" in request.user and v.headline in request.user
    assert "evidence_ids" not in request.user  # ids mean nothing to the reader


def test_a_version_is_explained_once(session: Session, store: S3Store, source: Source) -> None:
    v = published(session, store, source)
    provider = FakeProvider([answer(good_text(v))])
    adapter = adapter_for(session, provider)
    assert explain_version(session, adapter, v.id) == "explained"
    assert explain_version(session, adapter, v.id) == "skipped"
    assert len(provider.calls) == 1 and not needs_explanation(v)


def test_a_rejected_answer_is_retried_once_with_the_problems_appended(
    session: Session, store: S3Store, source: Source
) -> None:
    v = published(session, store, source)
    bad = "Prices rose 99.9% and Kano was worse."
    provider = FakeProvider([answer(bad), answer(good_text(v))])

    assert explain_version(session, adapter_for(session, provider), v.id) == "explained"

    first, second = provider.calls
    assert "rejected" not in first.user
    assert bad in second.user and "The number 99.9 is not a figure in the facts" in second.user
    assert "The place Kano is outside this situation's scope" in second.user
    assert v.explanation == good_text(v)


def test_after_two_failures_the_version_keeps_publishing_without_an_explanation(
    session: Session, store: S3Store, source: Source
) -> None:
    v = published(session, store, source)
    provider = FakeProvider([answer("It will definitely rise 99.9%."), "not json at all"])

    assert explain_version(session, adapter_for(session, provider), v.id) == "failed"

    assert len(provider.calls) == 2  # never a third
    assert v.explanation is None and v.status == "published"
    assert v.prompt_version == PROMPT_VERSION  # tried: the backfill leaves it alone
    assert not needs_explanation(v) and versions_missing_explanation(session, 10) == []
    assert session.scalar(select(func.count()).select_from(LlmCall)) == 2


def test_an_unusable_reply_counts_as_a_failed_attempt_not_a_crash(
    session: Session, store: S3Store, source: Source
) -> None:
    v = published(session, store, source)
    provider = FakeProvider(["{broken", answer(good_text(v))])
    assert explain_version(session, adapter_for(session, provider), v.id) == "explained"
    assert "not valid JSON" in provider.calls[1].user


def test_insufficient_evidence_cards_are_never_explained(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, PMS_OCT)
    place = session.scalars(select(Place.id).where(Place.code == "NG-LA")).one()
    (situation,) = ensure_situations(session, [("pms_litre", place)])
    v = assess_situation(session, situation.id, TODAY).version  # 2024 data, assessed in 2026
    assert v is not None and v.evidence_state == "insufficient"
    provider = FakeProvider([])
    assert explain_version(session, adapter_for(session, provider), v.id) == "skipped"
    assert provider.calls == []


def test_an_exhausted_budget_propagates_and_changes_nothing(
    session: Session, store: S3Store, source: Source
) -> None:
    v = published(session, store, source)
    provider = FakeProvider([])
    with pytest.raises(BudgetExhausted):
        explain_version(session, adapter_for(session, provider, budget=0), v.id)
    assert provider.calls == [] and v.prompt_version is None and needs_explanation(v)


def test_the_validator_sees_the_real_scope_parents_and_places(
    session: Session, store: S3Store, source: Source
) -> None:
    v = published(session, store, source)
    data = build_input(session, v)
    assert data.parent_places == () and "Nigeria" in data.known_places
    assert "Lagos" in data.known_places and "Abuja" in data.known_places
    assert any("Kano" in p for p in validate_explanation("Petrol in Kano rose.", data))
    state = session.scalars(select(Place).where(Place.code == "NG-LA")).one()
    assert explain_module._ancestors(session, state) == ["Nigeria"]


# --- the jobs ----------------------------------------------------------------------------------


_runs = itertools.count()


def run(session: Session, handler: Any, kind: str, payload: dict[str, Any]) -> int:
    job_id = enqueue(
        session, kind, payload, dedupe_key=f"test-run:{kind}:{json.dumps(payload)}:{next(_runs)}"
    )
    assert job_id is not None
    job = ClaimedJob(id=job_id, kind=kind, payload=payload, attempts=1, max_attempts=5)
    handler(JobContext(session=session, job=job, worker_id="test"))
    return job_id


def with_key(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider, budget: float = 10.0) -> None:
    monkeypatch.setattr(explain_jobs, "_has_api_key", lambda ctx: True)
    monkeypatch.setattr(
        explain_jobs, "build_adapter", lambda session: adapter_for(session, provider, budget)
    )


def test_the_job_writes_the_explanation(
    session: Session, store: S3Store, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = published(session, store, source)
    with_key(monkeypatch, FakeProvider([answer(good_text(v))]))
    run(session, explain_jobs.explain_version_job, "explain_version", {"version_id": v.id})
    assert v.explanation == good_text(v)


def test_without_an_api_key_the_job_does_nothing_and_the_backfill_waits(
    session: Session, store: S3Store, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = published(session, store, source)
    monkeypatch.setattr(explain_jobs, "_has_api_key", lambda ctx: False)
    monkeypatch.setattr(
        explain_jobs, "build_adapter", lambda session: pytest.fail("no key: no adapter")
    )
    run(session, explain_jobs.explain_version_job, "explain_version", {"version_id": v.id})
    run(session, explain_jobs.explain_backfill_job, "explain_backfill", {})
    assert v.explanation is None and v.prompt_version is None
    assert versions_missing_explanation(session, 10) == [v.id]


def test_a_spent_budget_defers_the_job_to_the_next_budget_day(
    session: Session, store: S3Store, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = published(session, store, source)
    with_key(monkeypatch, FakeProvider([]), budget=0)
    run(session, explain_jobs.explain_version_job, "explain_version", {"version_id": v.id})
    deferred = session.scalars(
        select(Job).where(Job.kind == "explain_version", Job.dedupe_key.like("%:after:%"))
    ).one()
    assert deferred.payload == {"version_id": v.id}
    assert deferred.run_at > TODAY and deferred.run_at <= TODAY + timedelta(days=1)
    assert v.explanation is None and v.prompt_version is None


def test_the_backfill_explains_current_versions_and_stops_when_the_budget_is_spent(
    session: Session, store: S3Store, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = published(session, store, source)
    provider = FakeProvider([answer(good_text(v))])
    with_key(monkeypatch, provider)
    run(session, explain_jobs.explain_backfill_job, "explain_backfill", {})
    assert v.explanation == good_text(v) and versions_missing_explanation(session, 10) == []

    w = published_other(session, store, source)
    with_key(monkeypatch, FakeProvider([]), budget=0)
    run(session, explain_jobs.explain_backfill_job, "explain_backfill", {})  # stops quietly
    assert w.explanation is None and versions_missing_explanation(session, 10) == [w.id]


def published_other(session: Session, store: S3Store, source: Source) -> AssessmentVersion:
    """A second published version: the same item in Lagos, a routine (non-high) case is hard to
    find in the fixtures, so the first version of the state situation is published directly."""
    place = session.scalars(select(Place.id).where(Place.code == "NG-LA")).one()
    (situation,) = ensure_situations(session, [("lpg_12_5kg", place)])
    v = assess_situation(session, situation.id, WHEN).version
    assert v is not None and v.evidence_state == "reported"
    v.status, v.published_at, situation.current_version_id = "published", WHEN, v.id
    session.flush()
    return v


# --- queued by assess_situation ----------------------------------------------------------------


class _Clock(datetime):
    moment = WHEN

    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return cls.moment


def assess_job(session: Session, situation: Situation) -> None:
    run(
        session, assess_handler.assess_situation, "assess_situation", {"situation_id": situation.id}
    )


def explain_jobs_queued(session: Session) -> int:
    return int(
        session.scalar(select(func.count()).select_from(Job).where(Job.kind == "explain_version"))
        or 0
    )


def national_situation(session: Session, store: S3Store, source: Source) -> Situation:
    import_bytes(session, store, source, GAS_OCT)
    place = session.scalars(select(Place.id).where(Place.code == "NG")).one()
    (situation,) = ensure_situations(session, [("lpg_12_5kg", place)])
    return situation


def test_a_published_version_queues_its_explanation(
    session: Session, store: S3Store, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assess_handler, "datetime", _Clock)
    situation = national_situation(session, store, source)
    assess_job(session, situation)
    job = session.scalars(select(Job).where(Job.kind == "explain_version")).one()
    version = session.scalars(
        select(AssessmentVersion).where(AssessmentVersion.situation_id == situation.id)
    ).one()
    assert version.status == "published" and job.payload == {"version_id": version.id}


def test_a_withheld_version_queues_nothing(
    session: Session, store: S3Store, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assess_handler, "datetime", _Clock)
    situation = national_situation(session, store, source)
    set_publication_suspended(session, True, WHEN)  # R1
    assess_job(session, situation)
    assert explain_jobs_queued(session) == 0


def test_an_insufficient_card_queues_nothing(
    session: Session, store: S3Store, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assess_handler, "datetime", _Clock)
    _Clock.moment = TODAY  # October 2024 data seen in September 2026
    try:
        assess_job(session, national_situation(session, store, source))
    finally:
        _Clock.moment = WHEN
    assert explain_jobs_queued(session) == 0


def test_the_stored_paragraph_is_the_cleaned_one(
    session: Session, store: S3Store, source: Source
) -> None:
    v = published(session, store, source)
    messy = good_text(v).replace(" on average", "​  on average") + "​ "
    provider = FakeProvider([answer(messy)])
    assert explain_version(session, adapter_for(session, provider), v.id) == "explained"
    assert v.explanation == good_text(v)
