"""Shared setup for the NERC and price announcement adapter tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from africasignal.models import Place, Source, SourcePermission
from africasignal.net.fetch import FetchResult
from africasignal.sources.base import AdapterContext
from africasignal.storage import S3Store
from tests.integration.nbs_support import make_store


def store_fixture() -> Iterator[S3Store]:
    yield from make_store()


def ctx(session: Session, store: S3Store) -> AdapterContext:
    return AdapterContext(session=session, store=store)


def add_source(
    session: Session,
    slug: str,
    *,
    adapter: str,
    home_url: str,
    feed_url: str | None = None,
    kind: str = "regulator",
    **permission: object,
) -> Source:
    source = Source(
        slug=slug,
        name=f"{slug} (test)",
        kind=kind,
        adapter=adapter,
        home_url=home_url,
        feed_url=feed_url,
        schedule_minutes=360,
        max_requests_per_hour=30,
    )
    session.add(source)
    session.flush()
    fields: dict[str, object] = {
        "version": 1,
        "may_collect": True,
        "may_store_full_text": True,
        "max_quote_chars": None,
        "may_republish_numbers": True,
        "approved_at": datetime.now(UTC),
    }
    fields.update(permission)
    session.add(SourcePermission(source_id=source.id, **fields))
    session.flush()
    return source


def add_country(session: Session) -> Place:
    place = Place(code="NG", name="Nigeria", kind="country")
    session.add(place)
    session.flush()
    return place


class FakeSite:
    """Serves canned bodies by URL and records every call."""

    def __init__(self) -> None:
        self.pages: dict[str, FetchResult] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def serve(self, url: str, content: bytes, content_type: str) -> None:
        self.pages[url] = FetchResult(
            url=url, status_code=200, headers={"content-type": content_type}, content=content
        )

    def __call__(self, url: str, **kwargs: object) -> FetchResult:
        self.calls.append((url, kwargs))
        return self.pages.get(url, FetchResult(url=url, status_code=404))
