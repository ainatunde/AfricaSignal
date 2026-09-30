"""Errors raised by the language-model layer."""

from __future__ import annotations

from datetime import datetime


class LlmError(Exception):
    """Base class: a model call did not produce a usable answer."""


class LlmNotConfigured(LlmError):
    """No API key is set, so no provider can be built."""


class BudgetExhausted(LlmError):
    """The day's spend has reached ``LLM_DAILY_BUDGET_USD``. Nothing was sent to the provider.

    ``retry_at`` is when the next budget day starts; callers reschedule their job for then
    (``africasignal.llm.budget.defer_until_next_day``).
    """

    def __init__(self, spent: object, limit: object, retry_at: datetime) -> None:
        super().__init__(f"daily LLM budget reached: spent {spent} of {limit} USD")
        self.retry_at = retry_at


class JobTokenLimitExceeded(LlmError):
    """The job has already used ``LLM_PER_JOB_MAX_TOKENS`` tokens."""


class UnknownModelPrice(LlmError):
    """The model has no entry in the price table, so its cost cannot be metered."""


class SchemaValidationError(LlmError):
    """The provider answered, but not with JSON that matches the schema. The call is still billed
    and recorded; nothing is cached."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("response does not match the schema: " + "; ".join(problems[:5]))
        self.problems = problems


class ProviderRefused(LlmError):
    """The model declined the request (``stop_reason: refusal``)."""
