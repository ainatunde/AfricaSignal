"""The LLM adapter against PostgreSQL, with the fake provider (AS-020)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from africasignal.jobs.queue import enqueue
from africasignal.llm import (
    BudgetExhausted,
    JobTokenLimitExceeded,
    LlmAdapter,
    ProviderRefused,
    SchemaValidationError,
    UnknownModelPrice,
)
from africasignal.llm.budget import budget_status, defer_until_next_day
from africasignal.llm.config import LlmConfig, ModelPrice
from africasignal.llm.fake import FakeProvider, FakeReply
from africasignal.models import Job, LlmCall, LlmResponseCache

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
# A fake call of 100 input and 50 output tokens costs (100 * 1 + 50 * 5) / 1e6 = 0.00035 USD.
CALL_COST = Decimal("0.00035")


def config(*, purpose_model: str | None = None) -> LlmConfig:
    return LlmConfig(
        default_model="fake-model",
        purposes={"special": {"model": purpose_model}} if purpose_model else {},  # type: ignore[arg-type]
        prices={
            "fake-model": ModelPrice(input=Decimal(1), output=Decimal(5)),
            "other-model": ModelPrice(input=Decimal(2), output=Decimal(10)),
        },
    )


def reply(answer: str = "ok", **kw: int) -> FakeReply:
    import json

    return FakeReply(json.dumps({"answer": answer}), input_tokens=100, output_tokens=50, **kw)


def adapter(
    session: Session,
    provider: FakeProvider,
    *,
    budget: float = 10.0,
    per_job: int = 20000,
    cfg: LlmConfig | None = None,
    now: datetime = NOW,
) -> LlmAdapter:
    return LlmAdapter(
        session,
        provider,
        config=cfg or config(),
        daily_budget_usd=budget,
        per_job_max_tokens=per_job,
        clock=lambda: now,
    )


def call(a: LlmAdapter, *, user: str = "u", version: str = "v1", purpose: str = "p", **kw):  # type: ignore[no-untyped-def]
    return a.complete_json(purpose, version, "system", user, SCHEMA, 1000, **kw)


def calls(session: Session) -> list[LlmCall]:
    return list(session.scalars(select(LlmCall).order_by(LlmCall.id)))


def test_a_call_returns_the_answer_and_records_its_cost(session: Session) -> None:
    fake = FakeProvider([reply("hello")])
    assert call(adapter(session, fake)) == {"answer": "hello"}
    (row,) = calls(session)
    assert (row.purpose, row.model_id, row.prompt_version) == ("p", "fake-model", "v1")
    assert (row.input_tokens, row.output_tokens, row.cost_usd) == (100, 50, CALL_COST)
    assert row.cache_hit is False and row.ts == NOW


def test_the_provider_gets_the_configured_model_effort_and_schema(session: Session) -> None:
    cfg = LlmConfig.model_validate(
        {
            "default_model": "fake-model",
            "purposes": {"p": {"model": "other-model", "effort": "low"}},
            "prices": {
                "fake-model": {"input": 1, "output": 5},
                "other-model": {"input": 2, "output": 10},
            },
        }
    )
    fake = FakeProvider([reply()])
    call(adapter(session, fake, cfg=cfg))
    (request,) = fake.calls
    assert (request.model, request.effort, request.schema) == ("other-model", "low", SCHEMA)
    assert (request.system, request.user, request.max_tokens) == ("system", "u", 1000)
    assert calls(session)[0].cost_usd == Decimal("0.0007")  # 100 * 2 + 50 * 10 = 700 micro-USD


def test_the_same_input_is_answered_from_the_cache(session: Session) -> None:
    fake = FakeProvider([reply("first")])
    a = adapter(session, fake)
    assert call(a) == {"answer": "first"}
    assert call(a) == {"answer": "first"}
    assert len(fake.calls) == 1
    first, second = calls(session)
    assert (first.cache_hit, second.cache_hit) == (False, True)
    assert (second.cost_usd, second.input_tokens, second.output_tokens) == (0, 0, 0)
    assert session.scalar(select(func.count()).select_from(LlmResponseCache)) == 1


def test_a_cached_answer_cannot_be_changed_by_the_caller(session: Session) -> None:
    a = adapter(session, FakeProvider([reply("first")]))
    call(a)["answer"] = "tampered"
    assert call(a) == {"answer": "first"}


@pytest.mark.parametrize(
    "change",
    [{"user": "other text"}, {"version": "v2"}, {"purpose": "special"}],
)
def test_a_different_input_version_or_purpose_misses_the_cache(
    session: Session, change: dict[str, str]
) -> None:
    fake = FakeProvider([reply(), reply()])
    a = adapter(session, fake)
    call(a)
    call(a, **change)
    assert len(fake.calls) == 2


def test_a_different_model_misses_the_cache(session: Session) -> None:
    fake = FakeProvider([reply(), reply()])
    call(adapter(session, fake, cfg=config()), purpose="special")
    call(adapter(session, fake, cfg=config(purpose_model="other-model")), purpose="special")
    assert [c.model for c in fake.calls] == ["fake-model", "other-model"]


def test_the_schema_is_part_of_the_cache_key(session: Session) -> None:
    fake = FakeProvider([reply(), reply()])
    a = adapter(session, fake)
    call(a)
    a.complete_json("p", "v1", "system", "u", {**SCHEMA, "description": "changed"}, 1000)
    assert len(fake.calls) == 2


def test_budget_exhaustion_stops_calls_and_names_the_next_day(session: Session) -> None:
    fake = FakeProvider([reply(), reply()])
    a = adapter(session, fake, budget=0.0003)  # one call (0.00035) passes the cap
    call(a, user="one")
    with pytest.raises(BudgetExhausted) as exc:
        call(a, user="two")
    assert len(fake.calls) == 1  # nothing was sent the second time
    # 12:00 UTC on 30 Sep is 13:00 in Lagos; the next Lagos day starts at 23:00 UTC.
    assert exc.value.retry_at == datetime(2026, 9, 30, 23, 0, tzinfo=UTC)
    assert budget_status(session, Decimal("0.0003"), NOW).exhausted


def test_a_cached_answer_is_still_served_after_the_budget_is_spent(session: Session) -> None:
    fake = FakeProvider([reply("kept")])
    a = adapter(session, fake, budget=0.0003)
    call(a, user="one")
    assert call(a, user="one") == {"answer": "kept"}


def test_the_budget_starts_again_on_the_next_lagos_day(session: Session) -> None:
    fake = FakeProvider([reply(), reply()])
    call(adapter(session, fake, budget=0.0003), user="one")
    tomorrow = NOW + timedelta(hours=12)  # 00:00 UTC on 1 Oct is 01:00 in Lagos: a new day
    call(adapter(session, fake, budget=0.0003, now=tomorrow), user="two")
    assert len(fake.calls) == 2


def test_deferring_creates_one_job_per_day_for_the_next_budget_day(session: Session) -> None:
    payload = {"document_id": 7}
    first = defer_until_next_day(session, "extract_claims", payload, dedupe_key="k:7", now=NOW)
    again = defer_until_next_day(session, "extract_claims", payload, dedupe_key="k:7", now=NOW)
    assert first is not None and again is None
    job = session.get(Job, first)
    assert job is not None
    assert (job.kind, job.payload, job.status) == ("extract_claims", payload, "queued")
    assert job.run_at == datetime(2026, 9, 30, 23, 0, tzinfo=UTC)
    assert job.dedupe_key == "k:7:after:2026-10-01"


@pytest.mark.parametrize(
    ("bad", "fragment"),
    [
        ("not json", "not valid JSON"),
        ('{"answer": 3}', "expected string"),
        ('{"other": "x"}', "missing"),
        ('["a"]', "expected object"),
    ],
)
def test_a_bad_answer_is_billed_recorded_and_never_cached(
    session: Session, bad: str, fragment: str
) -> None:
    fake = FakeProvider([FakeReply(bad), reply("good")])
    a = adapter(session, fake)
    with pytest.raises(SchemaValidationError, match=fragment):
        call(a)
    (row,) = calls(session)
    assert row.cost_usd > 0 and row.cache_hit is False
    assert session.scalar(select(func.count()).select_from(LlmResponseCache)) == 0
    assert call(a) == {"answer": "good"}  # asking again calls the provider again
    assert len(fake.calls) == 2


def test_an_answer_cut_off_at_max_tokens_is_a_schema_failure(session: Session) -> None:
    fake = FakeProvider([FakeReply('{"answer": "cut', stop_reason="max_tokens")])
    with pytest.raises(SchemaValidationError, match="max_tokens"):
        call(adapter(session, fake))
    assert len(calls(session)) == 1


def test_a_refusal_is_billed_and_raised(session: Session) -> None:
    fake = FakeProvider([FakeReply("", stop_reason="refusal")])
    with pytest.raises(ProviderRefused):
        call(adapter(session, fake))
    assert len(calls(session)) == 1


def test_a_model_without_a_price_is_refused_before_any_call(session: Session) -> None:
    cfg = config()
    cfg.purposes["p"] = cfg.purpose("special").model_copy(update={"model": "mystery"})
    fake = FakeProvider([reply()])
    with pytest.raises(UnknownModelPrice):
        call(adapter(session, fake, cfg=cfg))
    assert fake.calls == []


def test_a_job_cannot_exceed_its_token_allowance(session: Session) -> None:
    job_id = enqueue(session, "extract_claims", {})
    assert job_id is not None
    fake = FakeProvider([reply(), reply()])
    a = adapter(session, fake, per_job=200)  # each call uses 150 tokens
    call(a, user="one", job_id=job_id)
    assert fake.calls[0].max_tokens == 200  # not clamped: the allowance was 200, asked for 1000
    call(a, user="two", job_id=job_id)
    assert fake.calls[1].max_tokens == 50  # 200 - 150 left
    with pytest.raises(JobTokenLimitExceeded):
        call(a, user="three", job_id=job_id)
    assert len(fake.calls) == 2
    assert {c.job_id for c in calls(session)} == {job_id}


def test_spend_is_kept_even_if_the_caller_rolls_back_afterwards(engine: Engine) -> None:
    """The adapter commits, so a job that fails after the call still pays for what it used."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as s:
            call(adapter(s, FakeProvider([reply("kept")])))
            s.rollback()  # the calling job fails later and rolls back its own work
        with factory() as s:
            assert len(calls(s)) == 1
            assert s.scalar(select(func.count()).select_from(LlmResponseCache)) == 1
    finally:
        with factory() as s:
            s.execute(delete(LlmCall))
            s.execute(delete(LlmResponseCache))
            s.commit()


# --- keys and limits come from the console settings -------------------------------------------


@pytest.fixture
def operator(session: Session, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    for name in ("ANTHROPIC_API_KEY", "LLM_DAILY_BUDGET_USD", "LLM_PER_JOB_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    from tests.integration.test_admin_console import make_operator

    return make_operator(session).operator


def settings_adapter(session: Session, fake: FakeProvider) -> LlmAdapter:
    """An adapter that reads its limits from the settings store, as production does."""
    return LlmAdapter(session, fake, config=config(), clock=lambda: NOW)


def test_the_api_key_saved_in_the_console_wins_over_the_environment(
    session: Session, operator, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from africasignal import settings_store
    from africasignal.llm.adapter import make_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-environment")
    assert make_provider(session)._client.api_key == "sk-from-environment"  # type: ignore[attr-defined]
    settings_store.set_value(session, operator, "anthropic_api_key", "sk-from-console")
    assert make_provider(session)._client.api_key == "sk-from-console"  # type: ignore[attr-defined]


def test_no_key_anywhere_means_not_configured(session: Session, operator) -> None:  # type: ignore[no-untyped-def]
    from africasignal.llm import LlmNotConfigured
    from africasignal.llm.adapter import build_adapter

    with pytest.raises(LlmNotConfigured):
        build_adapter(session)


def test_a_budget_changed_in_the_console_applies_to_the_next_call(
    session: Session, operator
) -> None:  # type: ignore[no-untyped-def]
    from africasignal import settings_store

    fake = FakeProvider([reply(), reply()])
    a = settings_adapter(session, fake)
    assert a.daily_budget_usd == Decimal("10.0")  # the default, nothing saved yet
    settings_store.set_value(session, operator, "llm_daily_budget_usd", "0.0003")
    call(a, user="one")
    with pytest.raises(BudgetExhausted):
        call(a, user="two")
    settings_store.set_value(session, operator, "llm_daily_budget_usd", "5")  # same adapter
    call(a, user="two")
    assert len(fake.calls) == 2


def test_the_budget_from_the_environment_is_the_fallback(
    session: Session, operator, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "3.5")
    assert settings_adapter(session, FakeProvider([])).daily_budget_usd == Decimal("3.5")


def test_the_per_job_token_limit_comes_from_the_console(session: Session, operator) -> None:  # type: ignore[no-untyped-def]
    from africasignal import settings_store

    settings_store.set_value(session, operator, "llm_per_job_max_tokens", "1000")
    job_id = enqueue(session, "extract_claims", {})
    assert job_id is not None
    fake = FakeProvider([FakeReply('{"answer": "a"}', input_tokens=600, output_tokens=300)] * 2)
    a = settings_adapter(session, fake)
    assert a.per_job_max_tokens == 1000
    call(a, user="one", job_id=job_id)
    call(a, user="two", job_id=job_id)
    assert fake.calls[1].max_tokens == 100  # 1000 - 900 used by the first call
