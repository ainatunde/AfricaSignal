"""Typed provider and purpose routing loaded from config/llm.yaml."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from africasignal.config import config_dir

Effort = Literal["low", "medium", "high", "xhigh", "max"]
ProviderName = Literal["anthropic", "openai"]


class ModelPrice(BaseModel):
    """USD per million tokens; price provenance is maintained beside the configuration."""

    model_config = ConfigDict(extra="forbid")

    input: Decimal
    output: Decimal


class PurposeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    provider: ProviderName = "anthropic"
    effort: Effort | None = None


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_model: str
    default_provider: ProviderName = "anthropic"
    purposes: dict[str, PurposeConfig] = {}
    prices: dict[str, ModelPrice]

    @model_validator(mode="after")
    def _every_model_has_a_price(self) -> LlmConfig:
        used = {(self.default_provider, self.default_model)}
        used.update((p.provider, p.model) for p in self.purposes.values())
        missing = [
            f"{provider}/{model}"
            for provider, model in used
            if self.price_for(f"{provider}/{model}") is None
        ]
        if missing:
            raise ValueError(f"llm.yaml: no price for route(s) {sorted(missing)}")
        return self

    def purpose(self, name: str) -> PurposeConfig:
        """The model settings for a purpose; unlisted purposes use the configured default."""
        return self.purposes.get(name) or PurposeConfig(
            provider=self.default_provider, model=self.default_model
        )

    def price_for(self, route: str) -> ModelPrice | None:
        provider, separator, model = route.partition("/")
        if not separator:
            provider, model = "anthropic", provider
        return self.prices.get(f"{provider}/{model}") or (
            self.prices.get(model) if provider == "anthropic" else None
        )

    def route_options(self) -> list[tuple[str, str]]:
        routes: dict[str, str] = {}
        for key in self.prices:
            if "/" in key:
                provider, model = key.split("/", 1)
            else:
                provider, model = "anthropic", key
            routes[f"{provider}/{model}"] = f"{provider.title()} · {model}"
        return sorted(routes.items())


@lru_cache
def load_llm_config() -> LlmConfig:
    return LlmConfig.model_validate(yaml.safe_load((config_dir() / "llm.yaml").read_text()))
