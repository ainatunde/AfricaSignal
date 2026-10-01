"""Typed loader for ``config/llm.yaml``."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from africasignal.config import config_dir

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ModelPrice(BaseModel):
    """USD per million tokens."""

    model_config = ConfigDict(extra="forbid")

    input: Decimal
    output: Decimal


class PurposeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    effort: Effort | None = None


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_model: str
    purposes: dict[str, PurposeConfig] = {}
    prices: dict[str, ModelPrice]

    @model_validator(mode="after")
    def _every_model_has_a_price(self) -> LlmConfig:
        used = {self.default_model, *(p.model for p in self.purposes.values())}
        unpriced = used - set(self.prices)
        if unpriced:
            raise ValueError(f"llm.yaml: no price for model(s) {sorted(unpriced)}")
        return self

    def purpose(self, name: str) -> PurposeConfig:
        """The model settings for a purpose; purposes not listed use ``default_model``."""
        return self.purposes.get(name) or PurposeConfig(model=self.default_model)


@lru_cache
def load_llm_config() -> LlmConfig:
    return LlmConfig.model_validate(yaml.safe_load((config_dir() / "llm.yaml").read_text()))
