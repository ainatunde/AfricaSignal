"""Typed loaders for ``config/items.yaml`` and ``config/policies.yaml``."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from africasignal.config import config_dir

Topic = Literal["energy", "food"]


class Factor(BaseModel):
    """A possible driver of a price change. Shown as "supported" only when a valid claim links to
    it through one of its keywords."""

    model_config = ConfigDict(extra="forbid")

    code: str
    label: str
    keywords: list[str] = Field(min_length=1)


class Materiality(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mom_pct: float = Field(default=5.0, gt=0)
    yoy_pct: float = Field(default=20.0, gt=0)


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    label: str
    topic: Topic
    unit: str
    currency: str = "NGN"
    frequency: Literal["monthly", "adhoc"] = "monthly"
    nbs_labels: list[str] = Field(min_length=1)
    materiality: Materiality = Materiality()
    factors: list[Factor] = Field(default_factory=list)


class Items(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: dict[Topic, list[str]]
    items: list[Item]

    @model_validator(mode="after")
    def _unique_codes(self) -> Items:
        codes = [i.code for i in self.items]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate item codes in items.yaml")
        return self

    def item(self, code: str) -> Item:
        return next(i for i in self.items if i.code == code)


class PolicySeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    title: str
    topic: Topic
    unit: str
    primary_sources: list[str] = Field(min_length=1)  # source slugs
    scope: Literal["national", "states"]
    state_codes: list[str] = Field(default_factory=list)
    affected_groups: str

    @model_validator(mode="after")
    def _states_match_scope(self) -> PolicySeries:
        if (self.scope == "states") != bool(self.state_codes):
            raise ValueError(
                f"{self.code}: state_codes must be given exactly when scope is 'states'"
            )
        return self


class Policies(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series: list[PolicySeries]

    @model_validator(mode="after")
    def _unique_codes(self) -> Policies:
        codes = [s.code for s in self.series]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate policy series codes in policies.yaml")
        return self


@lru_cache
def load_items() -> Items:
    return Items.model_validate(yaml.safe_load((config_dir() / "items.yaml").read_text()))


@lru_cache
def load_policies() -> Policies:
    return Policies.model_validate(yaml.safe_load((config_dir() / "policies.yaml").read_text()))
