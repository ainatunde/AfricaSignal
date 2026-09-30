"""The sources a case may name (keys of ``DocumentSpec.source``)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceDef:
    name: str
    kind: str
    adapter: str
    owner: str | None = None


# The sources a case may name. Names are chosen so ``source_short_name`` gives a readable label.
SOURCES: dict[str, SourceDef] = {
    "nbs": SourceDef("National Bureau of Statistics", "official_statistics", "nbs",
                     "National Bureau of Statistics"),
    "nerc": SourceDef("Nigerian Electricity Regulatory Commission", "regulator", "nerc",
                      "Nigerian Electricity Regulatory Commission"),
    "nmdpra": SourceDef("Nigerian Midstream and Downstream Petroleum Regulatory Authority",
                        "regulator", "price_announcement",
                        "Nigerian Midstream and Downstream Petroleum Regulatory Authority"),
    "nnpc": SourceDef("Nigerian National Petroleum Company", "company", "price_announcement",
                      "Nigerian National Petroleum Company"),
    "punch": SourceDef("Punch", "news_outlet", "rss"),
    "vanguard": SourceDef("Vanguard", "news_outlet", "rss"),
    "thisday": SourceDef("ThisDay", "news_outlet", "rss"),
    "businessday": SourceDef("BusinessDay", "news_outlet", "rss"),
    "premiumtimes": SourceDef("Premium Times", "news_outlet", "rss"),
    "channels": SourceDef("Channels", "news_outlet", "rss"),
    "nairametrics": SourceDef("Nairametrics", "news_outlet", "rss"),
    "dailytrust": SourceDef("Daily Trust", "news_outlet", "rss"),
}  # fmt: skip
# Documents of these kinds are read by code (spreadsheets), not by the model.

# Documents of these kinds are read by code (spreadsheets), not by the model, and are never
# clustered by text, so their wording cannot leak between splits.
CODE_READ_KINDS = {"official_statistics"}
OFFICIAL_KINDS = {"official_statistics", "regulator", "government", "company"}
