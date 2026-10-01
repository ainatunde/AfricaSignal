"""Provider-neutral entry point for language-model calls (spec B12, AS-020).

Callers use ``LlmAdapter.complete_json``. It answers from the response cache when it can, refuses
when the day's budget is spent, records every real call in ``llm_call`` with its cost, and checks
the answer against the schema before returning it. The provider behind it (Anthropic in
production, ``FakeProvider`` in tests) only turns a prompt into text.

Production adapters commit an independent accounting session after each call, so billed spend
and cached answers survive a job failure without committing the job's own changes. Directly
constructed adapters require a dedicated accounting session.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from africasignal import settings_store
from africasignal.db import get_engine
from africasignal.llm import cache
from africasignal.llm.budget import (
    budget_status,
    cost_usd,
    next_budget_day_start,
    tokens_used_by_job,
)
from africasignal.llm.config import LlmConfig, load_llm_config
from africasignal.llm.errors import (
    BudgetExhausted,
    JobTokenLimitExceeded,
    LlmNotConfigured,
    ProviderRefused,
    SchemaValidationError,
    UnknownModelPrice,
)
from africasignal.llm.schema import errors as schema_errors
from africasignal.models import LlmCall

log = logging.getLogger("africasignal.llm")

# Used only if the settings store supplies nothing; its own defaults are the same numbers.
DEFAULT_DAILY_BUDGET_USD = 10.0
DEFAULT_PER_JOB_MAX_TOKENS = 20_000


@dataclass(frozen=True)
class ProviderResponse:
    """What a provider returns: the raw answer text and what it cost in tokens."""

    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None = None  # "end_turn", "max_tokens", "refusal", ...


class LlmProvider(Protocol):
    def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
        effort: str | None,
    ) -> ProviderResponse:
        """One model call that must answer in JSON matching ``schema``."""
        ...


def make_provider(session: Session) -> LlmProvider:
    """The production provider, with the key the operator saved in the console or else
    ``ANTHROPIC_API_KEY``. Raises ``LlmNotConfigured`` when neither is set."""
    api_key = settings_store.get(session, "anthropic_api_key")
    if not api_key:
        raise LlmNotConfigured("no Anthropic API key: set one in the console or ANTHROPIC_API_KEY")
    from africasignal.llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider(api_key=api_key)


class LlmAdapter:
    def __init__(
        self,
        session: Session,
        provider: LlmProvider,
        *,
        config: LlmConfig | None = None,
        accounting_factory: sessionmaker[Session] | None = None,
        daily_budget_usd: Decimal | float | None = None,
        per_job_max_tokens: int | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.accounting_factory = accounting_factory
        self.session = session
        self.provider = provider
        self.config = config or load_llm_config()
        self._daily_budget_override = daily_budget_usd
        self._per_job_override = per_job_max_tokens
        self.clock = clock

    # Read on every use, not at construction: a limit changed in the console applies to the next
    # call. The arguments above override the stored settings (tests).

    @property
    def daily_budget_usd(self) -> Decimal:
        if self._daily_budget_override is not None:
            return Decimal(str(self._daily_budget_override))
        value = settings_store.get_float(self.session, "llm_daily_budget_usd")
        return Decimal(str(DEFAULT_DAILY_BUDGET_USD if value is None else value))

    @property
    def per_job_max_tokens(self) -> int:
        if self._per_job_override is not None:
            return self._per_job_override
        value = settings_store.get_int(self.session, "llm_per_job_max_tokens")
        return DEFAULT_PER_JOB_MAX_TOKENS if value is None else value

    def model_for(self, purpose: str) -> str:
        return self.config.purpose(purpose).model

    def complete_json(
        self,
        purpose: str,
        prompt_version: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
        *,
        job_id: int | None = None,
    ) -> dict[str, Any]:
        """Ask the model for JSON matching ``schema`` and return it.

        Raises ``BudgetExhausted`` (nothing sent), ``JobTokenLimitExceeded``, ``ProviderRefused``
        or ``SchemaValidationError`` (both billed and recorded). ``job_id`` ties the spend to the
        running job and enforces ``LLM_PER_JOB_MAX_TOKENS`` across all of that job's calls.
        """
        if self.accounting_factory is not None:
            # Accounting commits must never commit unrelated writes made by a job handler.
            with self.accounting_factory() as accounting:
                adapter = LlmAdapter(
                    accounting,
                    self.provider,
                    config=self.config,
                    daily_budget_usd=self._daily_budget_override,
                    per_job_max_tokens=self._per_job_override,
                    clock=self.clock,
                )
                return adapter.complete_json(
                    purpose, prompt_version, system, user, schema, max_tokens, job_id=job_id
                )
        purpose_cfg = self.config.purpose(purpose)
        model = purpose_cfg.model
        price = self.config.prices.get(model)
        if price is None:
            raise UnknownModelPrice(f"no price for model {model!r} in llm.yaml")
        now = self.clock()
        sha = cache.input_sha256(system, user, schema)

        hit = cache.get(self.session, purpose, prompt_version, model, sha)
        if hit is not None:
            self._record(purpose, prompt_version, model, 0, 0, Decimal(0), job_id, now, True)
            self.session.commit()
            log.info("llm cache hit", extra={"purpose": purpose, "job_id": job_id})
            cached: dict[str, Any] = json.loads(json.dumps(hit.response))
            return cached

        status = budget_status(self.session, self.daily_budget_usd, now)
        if status.exhausted:
            raise BudgetExhausted(status.spent, status.limit, next_budget_day_start(now))

        if job_id is not None:
            remaining = self.per_job_max_tokens - tokens_used_by_job(self.session, job_id)
            if remaining <= 0:
                raise JobTokenLimitExceeded(
                    f"job {job_id} has used its {self.per_job_max_tokens} token allowance"
                )
            max_tokens = min(max_tokens, remaining)

        reply = self.provider.complete(
            model=model,
            system=system,
            user=user,
            schema=schema,
            max_tokens=max_tokens,
            effort=purpose_cfg.effort,
        )
        cost = cost_usd(price, reply.input_tokens, reply.output_tokens)
        self._record(
            purpose,
            prompt_version,
            model,
            reply.input_tokens,
            reply.output_tokens,
            cost,
            job_id,
            now,
            False,
        )
        try:
            data = self._parse(reply, schema)
        except (SchemaValidationError, ProviderRefused):
            self.session.commit()  # the tokens were billed whatever the answer looked like
            raise
        cache.put(
            self.session,
            purpose,
            prompt_version,
            model,
            sha,
            data,
            reply.input_tokens,
            reply.output_tokens,
        )
        self.session.commit()
        log.info(
            "llm call",
            extra={
                "purpose": purpose,
                "job_id": job_id,
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "cost_usd": str(cost),
            },
        )
        return data

    @staticmethod
    def _parse(reply: ProviderResponse, schema: dict[str, Any]) -> dict[str, Any]:
        if reply.stop_reason == "refusal":
            raise ProviderRefused("the model declined the request")
        if reply.stop_reason == "max_tokens":
            raise SchemaValidationError(["response was cut off at max_tokens"])
        try:
            data = json.loads(reply.text)
        except json.JSONDecodeError as exc:
            raise SchemaValidationError([f"not valid JSON: {exc}"]) from exc
        problems = schema_errors(data, schema)
        if problems or not isinstance(data, dict):
            raise SchemaValidationError(problems or ["top level is not an object"])
        return data

    def _record(
        self,
        purpose: str,
        prompt_version: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost: Decimal,
        job_id: int | None,
        ts: datetime,
        cache_hit: bool,
    ) -> None:
        self.session.add(
            LlmCall(
                purpose=purpose,
                model_id=model,
                prompt_version=prompt_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                job_id=job_id,
                ts=ts,
                cache_hit=cache_hit,
            )
        )
        self.session.flush()


def build_adapter(session: Session) -> LlmAdapter:
    """The adapter handlers use: the production provider, and the key and limits from the console
    settings (falling back to the environment)."""
    return LlmAdapter(
        session,
        make_provider(session),
        accounting_factory=sessionmaker(bind=get_engine(), expire_on_commit=False),
    )
