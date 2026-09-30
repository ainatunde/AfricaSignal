"""The adapter interface (spec B6.2) and the registry that maps a source's ``adapter`` to code."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from africasignal.models import EvidenceDocument, Source
from africasignal.storage import ObjectStore


@dataclass(frozen=True)
class DiscoveredItem:
    """A document a source lists: fetched later by ``process_document``."""

    url: str
    title: str | None = None
    published_at: datetime | None = None
    byline: str | None = None  # author or wire credit the listing gives, if any


@dataclass
class ProcessResult:
    measurements: int = 0
    claims: int = 0
    notes: list[str] = field(default_factory=list)
    # (item code, place id) pairs whose values changed: their situations need re-assessing
    touched: set[tuple[str, int]] = field(default_factory=set)
    # ids of measurements replaced by restated values: assessments using them are corrected
    superseded: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class AdapterContext:
    session: Session
    store: ObjectStore


class SourceAdapter(Protocol):
    slug_prefix: str

    def discover(self, source: Source, ctx: AdapterContext) -> list[DiscoveredItem]:
        """List the documents the source currently offers, fetching through ``fetch_document``."""
        ...

    def process(self, doc: EvidenceDocument, ctx: AdapterContext) -> ProcessResult:
        """Turn a captured document into measurements and/or claims."""
        ...


# Keyed by the ``source.adapter`` enum value: nbs, nerc, price_announcement, rss, gdelt.
ADAPTERS: dict[str, SourceAdapter] = {}


def register_adapter(name: str, adapter: SourceAdapter) -> None:
    if name in ADAPTERS:
        raise ValueError(f"adapter {name!r} is already registered")
    ADAPTERS[name] = adapter


def get_adapter(name: str) -> SourceAdapter | None:
    return ADAPTERS.get(name)
