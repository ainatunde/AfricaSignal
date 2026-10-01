"""Sources an operator approved, for the evidence tests (AS-027, security review S-08)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from africasignal.models import Source, SourcePermission

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
_DEFAULT = "default"


def approved_source(
    session: Session,
    slug: str,
    *,
    kind: str = "news_outlet",
    adapter: str = "rss",
    owner: str | None = _DEFAULT,
    home_url: str | None = None,
    approved_days_ago: int | None = 90,
    may_collect: bool = True,
    active: bool = True,
    now: datetime | None = None,
) -> Source:
    """A source with one approved permission, ``approved_days_ago`` days before ``now`` (None: never
    approved). A news outlet gets an owner of its own unless ``owner`` is given (None: no owner on
    record, which makes it untrusted)."""
    if owner == _DEFAULT:
        owner = f"Owner of {slug}" if kind == "news_outlet" else None
    source = Source(
        slug=slug,
        name=slug.title(),
        kind=kind,
        adapter=adapter,
        owner=owner,
        home_url=home_url,
        schedule_minutes=30,
        active=active,
    )
    session.add(source)
    session.flush()
    if approved_days_ago is not None:
        session.add(
            SourcePermission(
                source_id=source.id,
                version=1,
                may_collect=may_collect,
                may_store_full_text=True,
                may_republish_numbers=True,
                approved_at=(now or NOW) - timedelta(days=approved_days_ago),
            )
        )
        session.flush()
    return source
