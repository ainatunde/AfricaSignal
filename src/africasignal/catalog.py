"""Typed loaders for ``config/items.yaml`` and ``config/policies.yaml``."""

from __future__ import annotations

import re
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


NbsCode = Literal["pms", "ago", "dpk", "lpg", "food"]
NbsLayout = Literal["state_table", "zoned_table", "two_blocks", "national_items"]


class NbsPublication(BaseModel):
    """One NBS price-watch publication: how to recognise it in the eLibrary and how to read it."""

    model_config = ConfigDict(extra="forbid")

    code: NbsCode
    layout: NbsLayout
    title_pattern: str  # regular expression matched against the eLibrary listing title

    @model_validator(mode="after")
    def _pattern_compiles(self) -> NbsPublication:
        re.compile(self.title_pattern)
        return self

    def matches(self, title: str) -> bool:
        return re.search(self.title_pattern, title.strip(), re.IGNORECASE) is not None


class NbsMapping(BaseModel):
    """Where an item is found in NBS files: a publication, plus the block (state tables with two
    blocks) or the exact row labels (the national food table)."""

    model_config = ConfigDict(extra="forbid")

    publication: NbsCode
    block: str | None = None
    labels: list[str] = Field(default_factory=list)
    unit_confirmed: bool = True  # False when the NBS text does not state the unit

    @model_validator(mode="after")
    def _block_or_labels_match_publication(self) -> NbsMapping:
        if self.publication == "food":
            if not self.labels or self.block:
                raise ValueError("food items need `labels` and no `block`")
        elif self.labels:
            raise ValueError("only food items are found by row label")
        if self.publication in ("dpk", "lpg") and not self.block:
            raise ValueError(f"{self.publication} items need a `block`")
        if self.publication in ("pms", "ago") and self.block:
            raise ValueError(f"{self.publication} has a single table, so no `block`")
        return self


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    label: str
    topic: Topic
    unit: str
    currency: str = "NGN"
    frequency: Literal["monthly", "adhoc"] = "monthly"
    nbs: NbsMapping
    materiality: Materiality = Materiality()
    factors: list[Factor] = Field(default_factory=list)


class Items(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nbs_publications: list[NbsPublication]
    keywords: dict[Topic, list[str]]
    items: list[Item]

    @model_validator(mode="after")
    def _unique_codes(self) -> Items:
        codes = [i.code for i in self.items]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate item codes in items.yaml")
        return self

    @model_validator(mode="after")
    def _items_use_known_publications(self) -> Items:
        known = {p.code for p in self.nbs_publications}
        if len(known) != len(self.nbs_publications):
            raise ValueError("duplicate nbs publication codes in items.yaml")
        unknown = {i.nbs.publication for i in self.items} - known
        if unknown:
            raise ValueError(f"items use nbs publications that are not defined: {sorted(unknown)}")
        return self

    def item(self, code: str) -> Item:
        return next(i for i in self.items if i.code == code)

    def publication(self, code: str) -> NbsPublication:
        return next(p for p in self.nbs_publications if p.code == code)

    def publication_for_title(self, title: str) -> NbsPublication | None:
        """The NBS publication an eLibrary listing title belongs to, if it is one we track."""
        return next((p for p in self.nbs_publications if p.matches(title)), None)

    def items_for(self, publication: str) -> list[Item]:
        return [i for i in self.items if i.nbs.publication == publication]


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
