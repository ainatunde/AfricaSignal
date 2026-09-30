"""Language-model access behind one adapter (spec B12, AS-020)."""

from africasignal.llm.adapter import LlmAdapter, LlmProvider, ProviderResponse, build_adapter
from africasignal.llm.errors import (
    BudgetExhausted,
    JobTokenLimitExceeded,
    LlmError,
    LlmNotConfigured,
    ProviderRefused,
    SchemaValidationError,
    UnknownModelPrice,
)

__all__ = [
    "BudgetExhausted",
    "JobTokenLimitExceeded",
    "LlmAdapter",
    "LlmError",
    "LlmNotConfigured",
    "LlmProvider",
    "ProviderRefused",
    "ProviderResponse",
    "SchemaValidationError",
    "UnknownModelPrice",
    "build_adapter",
]
