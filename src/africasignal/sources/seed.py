"""Seed the source registry from ``config/sources.yaml`` (idempotent upsert by slug).

The seed owns a source's descriptive fields. It never overrides operational state (``active``
after the first insert, ``next_due_at``, ``health``) and never touches an existing permission:
seeded permissions are created unapproved as version 1, and only an operator approves them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.config import config_dir
from africasignal.models import Source, SourcePermission

log = logging.getLogger("africasignal.sources.seed")

SourceKind = Literal[
    "official_statistics", "regulator", "government", "company", "news_outlet", "aggregator"
]
AdapterName = Literal["nbs", "nerc", "price_announcement", "rss", "gdelt"]

# Descriptive fields the seed keeps in step with the file.
_MANAGED_FIELDS = (
    "name",
    "kind",
    "adapter",
    "owner",
    "home_url",
    "feed_url",
    "languages",
    "coverage_note",
    "schedule_minutes",
    "max_requests_per_hour",
)


class SeedPermission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    may_collect: bool
    may_store_full_text: bool
    max_quote_chars: int | None = None  # null = unlimited
    may_republish_numbers: bool
    link_required: bool = True
    retention_days: int | None = None
    terms_url: str | None = None
    rights_basis: str | None = None


class SeedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    kind: SourceKind
    adapter: AdapterName
    owner: str | None = None
    home_url: str | None = None
    feed_url: str | None = None
    languages: list[str] = Field(default_factory=lambda: ["en"])
    coverage_note: str | None = None
    schedule_minutes: int = Field(gt=0)
    max_requests_per_hour: int = Field(default=60, gt=0)
    active: bool = True
    permission: SeedPermission | None = None


@dataclass
class SeedResult:
    created: int = 0
    updated: int = 0
    permissions_created: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.permissions_created)


def load_seed(path: Path | None = None) -> list[SeedSource]:
    raw = yaml.safe_load((path or config_dir() / "sources.yaml").read_text())
    sources = [SeedSource.model_validate(entry) for entry in raw]
    slugs = [s.slug for s in sources]
    if len(slugs) != len(set(slugs)):
        raise ValueError("duplicate slug in sources.yaml")
    return sources


def seed_sources(
    session: Session, sources: list[SeedSource], now: datetime | None = None
) -> SeedResult:
    """Upsert ``sources``. Running it twice changes nothing the second time."""
    now = now or datetime.now(UTC)
    result = SeedResult()
    for seed in sources:
        source = session.scalars(select(Source).where(Source.slug == seed.slug)).first()
        if source is None:
            source = Source(
                slug=seed.slug,
                active=seed.active,
                next_due_at=now,
                **seed.model_dump(include=set(_MANAGED_FIELDS)),
            )
            session.add(source)
            session.flush()
            result.created += 1
        else:
            desired = seed.model_dump(include=set(_MANAGED_FIELDS))
            changed = {k: v for k, v in desired.items() if getattr(source, k) != v}
            for key, value in changed.items():
                setattr(source, key, value)
            if changed:
                result.updated += 1

        has_permission = session.scalars(
            select(SourcePermission.id).where(SourcePermission.source_id == source.id).limit(1)
        ).first()
        if seed.permission is not None and has_permission is None:
            session.add(
                SourcePermission(
                    source_id=source.id,
                    version=1,
                    approved_at=None,  # an operator approves it in the console
                    **seed.permission.model_dump(),
                )
            )
            result.permissions_created += 1
    session.flush()
    return result


def main() -> None:
    from africasignal.db import session_scope
    from africasignal.jobs.log import configure_logging

    configure_logging()
    with session_scope() as session:
        result = seed_sources(session, load_seed())
    log.info("seed done: %s", result)


if __name__ == "__main__":
    main()
