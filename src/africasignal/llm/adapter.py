"""Provider-neutral entry point for language-model calls (spec B12, AS-020).

Callers use ``LlmAdapter.complete_json``. It answers from the response cache when it can, refuses
when the day's budget is spent, records every real call in ``llm_call`` with its cost, and checks
the answer against the schema before returning it. The provider behind it (Anthropic in
production, ``FakeProvider`` in tests) only turns a prompt into text.

The adapter commits the session after each call so that spend and cached answers survive a later
failure of the calling job. Make the call before other uncommitted changes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.orm import Session

from africasignal.config import get_settings
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


def make_provider() -> LlmProvider:
    """The production provider. Raises ``LlmNotConfigured`` when there is no API key."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise LlmNotConfigured("ANTHROPIC_API_KEY is not set")
    from africasignal.llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider(api_key=settings.anthropic_api_key)


class LlmAdapter:
    def __init__(
        self,
        session: Session,
        provider: LlmProvider,
        *,
        config: LlmConfig | None = None,
        daily_budget_usd: Decimal | float | None = None,
        per_job_max_tokens: int | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        settings = get_settings()
        self.session = session
        self.provider = provider
        self.config = config or load_llm_config()
        self.daily_budget_usd = Decimal(
            str(settings.llm_daily_budget_usd if daily_budget_usd is None else daily_budget_usd)
        )
        self.per_job_max_tokens = (
            settings.llm_per_job_max_tokens if per_job_max_tokens is None else per_job_max_tokens
        )
        self.clock = clock

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
    """The adapter handlers use: the production provider and the settings from the environment."""
    return LlmAdapter(session, make_provider())
