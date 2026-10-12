"""The RSS adapter end to end through the job queue (AS-023): fetch_source -> process_document ->
extract_claims. Real worker and PostgreSQL; the "outlet" is a fake site serving a saved feed.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from africasignal.evidence.capture import capture
from africasignal.jobs import handlers, queue
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.handlers import fetch_source as fetch_source_module
from africasignal.jobs.handlers import process_document as process_document_module
from africasignal.jobs.worker import Worker
from africasignal.models import EvidenceDocument, ReportingOrigin, Source, SourcePermission
from africasignal.net.fetch import FetchResult
from africasignal.sources import base
from africasignal.sources.rss import RssAdapter, parse_feed
from africasignal.storage import S3Store
from tests.integration.nbs_support import make_store

TABLES = "evidence_document, reporting_origin, source_permission, source, job, job_deduplication"
FEED = Path(__file__).resolve().parents[1] / "fixtures" / "rss" / "nairametrics.xml"
FEED_URL = "https://nairametrics.com/feed/"

ARTICLE = """<html><head><title>{title}</title></head><body><article><h1>{title}</h1>
<p>{body}</p></article></body></html>"""
SENTENCE = (
    "The Nigerian National Petroleum Company said on Tuesday that the pump price of petrol and the "
    "electricity subsidy would be reviewed after talks with marketers in Lagos and Abuja. "
)


class Outlet:
    """Serves the saved feed and a long article for each of its items; records every request."""

    def __init__(self) -> None:
        self.entries = parse_feed(FEED.read_bytes(), FEED_URL)
        self.requests: list[str] = []
        self.feed = FEED.read_bytes()
        self.down = False

    def __call__(self, url: str, **kwargs: Any) -> FetchResult:
        self.requests.append(url)
        if self.down:
            return FetchResult(url=url, status_code=503)
        if url == FEED_URL:
            return FetchResult(
                url=url,
                status_code=200,
                headers={"content-type": "application/rss+xml"},
                content=self.feed,
            )
        entry = next(e for e in self.entries if e["url"] == url)
        # each story has its own words, or reporting-origin clustering would (rightly) treat the
        # four as copies of one wire story
        body = SENTENCE + " ".join(f"{entry['title']} ({i}) {entry['url']}." for i in range(12))
        page = ARTICLE.format(title=entry["title"], body=body)
        return FetchResult(
            url=url,
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=page.encode(),
        )

    @property
    def article_requests(self) -> list[str]:
        return [u for u in self.requests if u != FEED_URL]


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield sessionmaker(bind=engine, expire_on_commit=False)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from make_store()


@pytest.fixture
def outlet() -> Outlet:
    return Outlet()


@pytest.fixture
def extracted() -> list[int]:
    return []


@pytest.fixture(autouse=True)
def wired(
    monkeypatch: pytest.MonkeyPatch, store: S3Store, outlet: Outlet, extracted: list[int]
) -> None:
    def extract_claims(ctx: JobContext) -> None:  # AS-021 supplies the real one
        extracted.append(ctx.job.payload["document_id"])

    monkeypatch.setitem(handlers.HANDLERS, "fetch_source", fetch_source_module.fetch_source)
    monkeypatch.setitem(
        handlers.HANDLERS, "process_document", process_document_module.process_document
    )
    monkeypatch.setitem(handlers.HANDLERS, "extract_claims", extract_claims)
    monkeypatch.setattr(base, "ADAPTERS", {"rss": RssAdapter(fetch=outlet)})
    for module in (fetch_source_module, process_document_module):
        monkeypatch.setattr(module, "get_adapter", lambda name: base.ADAPTERS.get(name))
        monkeypatch.setattr(module, "get_store", lambda: store)
    monkeypatch.setattr(
        process_document_module, "capture", functools.partial(capture, fetch=outlet)
    )


def _setup(factory: sessionmaker[Session], *, approved: bool = True, active: bool = True) -> int:
    with factory() as s:
        source = Source(
            slug="nairametrics-rss", name="Nairametrics", kind="news_outlet", adapter="rss",
            home_url="https://nairametrics.com", feed_url=FEED_URL, schedule_minutes=30,
            max_requests_per_hour=60, active=active,
        )  # fmt: skip
        s.add(source)
        s.flush()
        s.add(
            SourcePermission(
                source_id=source.id,
                version=1,
                may_collect=True,
                may_store_full_text=False,
                max_quote_chars=300,
                may_republish_numbers=False,
                approved_at=datetime.now(UTC) if approved else None,
            )  # fmt: skip
        )
        s.commit()
        return source.id


def _drain(factory: sessionmaker[Session], limit: int = 100) -> int:
    worker = Worker(factory, "test-worker")
    ran = 0
    while ran < limit and worker.run_once():
        ran += 1
    return ran


def _fetch_source(factory: sessionmaker[Session], source_id: int) -> None:
    with factory() as s:
        queue.enqueue(s, "fetch_source", {"source_id": source_id})
        s.commit()


def _job_states(factory: sessionmaker[Session]) -> dict[tuple[str, str], int]:
    with factory() as s:
        rows = s.execute(text("SELECT kind, status, count(*) FROM job GROUP BY 1, 2")).all()
    return {(r[0], r[1]): r[2] for r in rows}


def _count(factory: sessionmaker[Session], model: type) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


def test_only_the_keyword_items_are_fetched_and_each_is_captured_once(
    factory: sessionmaker[Session], outlet: Outlet, extracted: list[int]
) -> None:
    source_id = _setup(factory)
    _fetch_source(factory, source_id)
    _drain(factory)

    # 20 items in the feed, 4 about our topics: only those 4 articles were requested.
    assert len(outlet.entries) == 20
    assert len(outlet.article_requests) == 4
    assert outlet.requests.count(FEED_URL) == 1
    assert _job_states(factory) == {
        ("fetch_source", "done"): 1,
        ("process_document", "done"): 4,
        ("extract_claims", "done"): 4,
    }
    with factory() as s:
        docs = s.scalars(select(EvidenceDocument).order_by(EvidenceDocument.id)).all()
        src = s.get(Source, source_id)
    assert len(docs) == 4
    assert sorted(extracted) == sorted(d.id for d in docs)
    assert src is not None and src.health == "healthy" and src.last_error is None


def test_captured_news_keeps_no_full_text_and_only_a_short_excerpt(
    factory: sessionmaker[Session],
) -> None:
    _fetch_source(factory, _setup(factory))
    _drain(factory)
    with factory() as s:
        docs = s.scalars(select(EvidenceDocument)).all()
    assert docs
    for d in docs:
        assert d.text_content is None
        assert d.excerpt is not None and 0 < len(d.excerpt) <= 300
        assert d.published_at is not None and d.title
        assert d.simhash is not None


def test_the_feeds_author_is_kept_as_the_documents_byline(
    factory: sessionmaker[Session], outlet: Outlet
) -> None:
    _fetch_source(factory, _setup(factory))
    _drain(factory)
    feed_bylines = {e["url"]: e["byline"] for e in outlet.entries}
    with factory() as s:
        docs = s.scalars(select(EvidenceDocument)).all()
    assert docs and all(d.byline for d in docs)
    assert {d.byline for d in docs} <= {b for b in feed_bylines.values() if b}


def test_each_article_gets_an_outlet_report_origin(factory: sessionmaker[Session]) -> None:
    _fetch_source(factory, _setup(factory))
    _drain(factory)
    with factory() as s:
        docs = s.scalars(select(EvidenceDocument)).all()
        origins = {o.id: o for o in s.scalars(select(ReportingOrigin)).all()}
    assert len(origins) == 4
    assert {d.origin_id for d in docs} == set(origins)
    for o in origins.values():
        assert o.kind == "outlet_report" and o.label and o.label.startswith("Nairametrics report, ")


def test_polling_again_changes_nothing(
    factory: sessionmaker[Session], outlet: Outlet, extracted: list[int]
) -> None:
    source_id = _setup(factory)
    _fetch_source(factory, source_id)
    _drain(factory)
    before = list(outlet.requests)
    with factory() as s:  # the next scheduled poll: a fresh fetch_source job
        s.execute(text("UPDATE job SET dedupe_key = NULL WHERE kind = 'fetch_source'"))
        s.commit()
    _fetch_source(factory, source_id)
    _drain(factory)
    assert outlet.requests[len(before) :] == [FEED_URL]  # the feed only: no article again
    assert _count(factory, EvidenceDocument) == 4
    assert _count(factory, ReportingOrigin) == 4
    assert len(extracted) == 4


def test_a_second_capture_of_a_url_reuses_the_origin_of_the_first(
    factory: sessionmaker[Session], store: S3Store, outlet: Outlet
) -> None:
    """A GDELT discovery of a story the feed also listed is one reporting origin."""
    source_id = _setup(factory)
    _fetch_source(factory, source_id)
    _drain(factory)
    with factory() as s:
        first = s.scalars(select(EvidenceDocument).order_by(EvidenceDocument.id)).first()
        other = Source(
            slug="other", name="Other", kind="aggregator", adapter="rss", schedule_minutes=30
        )
        s.add(other)
        s.flush()
        s.add(
            SourcePermission(
                source_id=other.id,
                version=1,
                may_collect=True,
                may_store_full_text=False,
                max_quote_chars=300,
                may_republish_numbers=False,
                approved_at=datetime.now(UTC),
            )  # fmt: skip
        )
        assert first is not None
        copy = capture(s, store, other, first.url, fetch=outlet)
        assert copy.id != first.id and copy.origin_id is None
        result = RssAdapter(fetch=outlet).process(copy, base.AdapterContext(s, store))
        assert copy.origin_id == first.origin_id
        assert any("reporting origin" in n for n in result.notes)


def test_a_feed_outage_marks_the_source_failing_and_fetches_no_articles(
    factory: sessionmaker[Session], outlet: Outlet
) -> None:
    source_id = _setup(factory)
    outlet.down = True
    _fetch_source(factory, source_id)
    _drain(factory, limit=1)
    with factory() as s:
        src = s.get(Source, source_id)
    assert src is not None and src.consecutive_failures == 1
    assert src.last_error is not None and "503" in src.last_error
    assert outlet.article_requests == []
    assert _count(factory, EvidenceDocument) == 0


def test_a_source_without_approved_terms_fetches_nothing(
    factory: sessionmaker[Session], outlet: Outlet
) -> None:
    _fetch_source(factory, _setup(factory, approved=False))
    _drain(factory)
    assert outlet.requests == []
    assert _count(factory, EvidenceDocument) == 0


def test_an_inactive_source_fetches_nothing(factory: sessionmaker[Session], outlet: Outlet) -> None:
    _fetch_source(factory, _setup(factory, active=False))
    _drain(factory)
    assert outlet.requests == []
