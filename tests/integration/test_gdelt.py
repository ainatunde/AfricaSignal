"""The GDELT adapter end to end (AS-024): real Events and Mentions files from tests/fixtures/gdelt,
a real PostgreSQL, a fake network and in-memory storage."""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from africasignal.evidence.capture import record_document
from africasignal.jobs import handlers, queue
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.handlers import gdelt_fetch_article as fetch_article_handler
from africasignal.jobs.handlers import gdelt_poll as poll_handler
from africasignal.jobs.worker import Worker
from africasignal.models import EvidenceDocument, GdeltDiscovery, Setting, Source, SourcePermission
from africasignal.net.fetch import FetchResult
from africasignal.sources import gdelt
from africasignal.storage import S3Store
from tests.integration.nbs_support import make_store
from tests.unit.sources.test_gdelt_parse import export_row

FIXTURES = Path(__file__).parents[1] / "fixtures" / "gdelt"
LATEST = "20260930180000"
TABLES = "gdelt_discovery, evidence_document, reporting_origin, source_permission, source, job, job_deduplication, setting"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_zip(name: str, body: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, body)
    return buffer.getvalue()


def mention_row(event_id: int, url: str, *, ts: str = LATEST, kind: str = "1") -> str:
    cells = [""] * gdelt.MENTION_COLUMNS
    cells[gdelt.MEN_EVENT_ID], cells[gdelt.MEN_MENTION_TS] = str(event_id), ts
    cells[gdelt.MEN_TYPE], cells[gdelt.MEN_IDENTIFIER] = kind, url
    return "\t".join(cells)


def html_page(title: str) -> bytes:
    return (
        f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>"
        "<p>Synthetic test text that is long enough to be extracted as the article body.</p>"
        "</article></body></html>"
    ).encode()


class Network:
    """Stands in for ``fetch_document``: GDELT files by URL, article pages by URL."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.statuses: dict[str, int] = {}  # URL -> non-200 status
        self.content_types: dict[str, str] = {}
        self.calls: list[str] = []

    def add_window(self, timestamp: str, export_body: str, mentions_body: str) -> None:
        export_url, mentions_url = gdelt.window_urls(timestamp)
        self.files[export_url] = make_zip(f"{timestamp}.export.CSV", export_body)
        self.files[mentions_url] = make_zip(f"{timestamp}.mentions.CSV", mentions_body)

    def add_real_latest(self) -> None:
        self.files[gdelt.LASTUPDATE_URL] = fixture_bytes("lastupdate.txt")
        for name in ("export", "mentions"):
            url = f"{gdelt.BASE_URL}/{LATEST}.{name}.CSV.zip"
            self.files[url] = fixture_bytes(f"{LATEST}.{name}.CSV.zip")

    def set_lastupdate(self, timestamp: str) -> None:
        """A lastupdate.txt for a synthetic window, with the real size and MD5 of its files."""
        lines = []
        for name in ("export", "mentions"):
            url = f"{gdelt.BASE_URL}/{timestamp}.{name}.CSV.zip"
            data = self.files[url]
            lines.append(
                f"{len(data)} {hashlib.md5(data).hexdigest()} {url.replace('https:', 'http:')}"  # noqa: S324
            )
        self.files[gdelt.LASTUPDATE_URL] = ("\n".join(lines) + "\n").encode()

    def __call__(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append(url)
        if url in self.statuses:
            return FetchResult(
                url=url, status_code=self.statuses[url], error=f"HTTP {self.statuses[url]}"
            )
        if url in self.files:
            headers = {"content-type": self.content_types.get(url, "application/octet-stream")}
            return FetchResult(url=url, status_code=200, headers=headers, content=self.files[url])
        return FetchResult(url=url, status_code=404)


def add_source(
    session: Session,
    slug: str,
    *,
    kind: str,
    adapter: str,
    home: str,
    approved: bool = True,
    full_text: bool = False,
    active: bool = True,
) -> Source:
    source = Source(
        slug=slug, name=slug, kind=kind, adapter=adapter, home_url=home, feed_url=home + "/feed/",
        schedule_minutes=15, max_requests_per_hour=30, active=active,
    )  # fmt: skip
    session.add(source)
    session.flush()
    session.add(
        SourcePermission(
            source_id=source.id, version=1, may_collect=True, may_store_full_text=full_text,
            max_quote_chars=300, may_republish_numbers=False,
            approved_at=datetime.now(UTC) if approved else None,
        )
    )  # fmt: skip
    session.flush()
    return source


def add_gdelt(session: Session, **kwargs: Any) -> Source:
    return add_source(
        session, "gdelt", kind="aggregator", adapter="gdelt", home="https://www.gdeltproject.org",
        **kwargs,
    )  # fmt: skip


def add_outlet(session: Session, slug: str, home: str, **kwargs: Any) -> Source:
    return add_source(session, slug, kind="news_outlet", adapter="rss", home=home, **kwargs)


def discoveries(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(GdeltDiscovery)) or 0


def job_count(session: Session, kind: str) -> int:
    return int(session.scalar(text("SELECT count(*) FROM job WHERE kind = :k"), {"k": kind}) or 0)


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from make_store()


@pytest.fixture
def net() -> Network:
    return Network()


@pytest.fixture
def real_window() -> tuple[str, str]:
    return tuple(  # type: ignore[return-value]
        gdelt._unzip_single(fixture_bytes(f"{LATEST}.{name}.CSV.zip"), name)
        for name in ("export", "mentions")
    )


# --- recording discoveries from the real window --------------------------------------------------


def test_the_real_window_records_one_row_per_nigeria_event_and_url(
    session: Session, real_window: tuple[str, str]
) -> None:
    add_outlet(session, "premiumtimes", "https://www.premiumtimesng.com")
    result = gdelt.process_window(session, LATEST, *real_window)

    assert result.events == 46
    assert result.mentions == 46  # the fixture has one web mention for each of the 46 events
    assert result.new == 46 == discoveries(session)
    row = session.scalars(
        select(GdeltDiscovery).where(GdeltDiscovery.global_event_id == 1325671542)
    ).one()
    assert row.mention_identifier.startswith("https://www.premiumtimesng.com/promoted/913606-")
    assert row.mention_ts == datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
    assert row.action_geo_country == "NI"
    assert row.evidence_document_id is None


def test_only_urls_of_approved_outlets_are_queued_and_other_domains_are_reported(
    session: Session, real_window: tuple[str, str]
) -> None:
    add_outlet(session, "premiumtimes", "https://www.premiumtimesng.com")
    add_outlet(session, "leadership", "https://leadership.ng")
    add_outlet(session, "punch", "https://punchng.com", approved=False)  # not approved
    result = gdelt.process_window(session, LATEST, *real_window)

    urls = [r for (r,) in session.execute(select(GdeltDiscovery.mention_identifier)).all()]
    approved = {gdelt.host_of(u) for u in urls} & {"premiumtimesng.com", "leadership.ng"}
    assert approved == {"premiumtimesng.com", "leadership.ng"}
    wanted = {u for u in urls if gdelt.host_of(u) in approved}
    jobs = session.execute(text("SELECT payload FROM job WHERE kind = 'gdelt_fetch_article'")).all()
    assert {p["url"] for (p,) in jobs} == wanted
    assert result.queued == len(wanted) > 0
    assert result.unknown_domain == len(set(urls)) - len(wanted) > 0

    report = {d.domain: d for d in gdelt.discovered_domains(session)}
    assert "nationalnetworkonline.com" in report and report["nationalnetworkonline.com"].urls >= 3
    assert "premiumtimesng.com" not in report and "leadership.ng" not in report
    assert report["nationalnetworkonline.com"].last_seen == datetime(2026, 9, 30, 18, 0, tzinfo=UTC)


def test_replaying_the_same_window_twice_creates_nothing_new(
    session: Session, real_window: tuple[str, str]
) -> None:
    add_outlet(session, "premiumtimes", "https://www.premiumtimesng.com")
    first = gdelt.process_window(session, LATEST, *real_window)
    jobs_after_first = job_count(session, "gdelt_fetch_article")
    second = gdelt.process_window(session, LATEST, *real_window)

    assert first.new == 46 and first.queued == jobs_after_first > 0
    assert (second.new, second.queued, second.linked, second.unknown_domain) == (0, 0, 0, 0)
    assert discoveries(session) == 46
    assert job_count(session, "gdelt_fetch_article") == jobs_after_first


def test_a_url_mentioned_for_several_events_is_fetched_once(
    session: Session, real_window: tuple[str, str]
) -> None:
    add_outlet(session, "national-network", "https://nationalnetworkonline.com")
    gdelt.process_window(session, LATEST, *real_window)
    urls = session.execute(
        text(
            "SELECT mention_identifier, count(*) FROM gdelt_discovery "
            "WHERE mention_identifier LIKE '%rivers-seeks-stronger-trade-ties-with-norway%' GROUP BY 1"
        )
    ).all()
    assert urls and urls[0][1] >= 2  # mentioned for at least two events in the fixture
    queued = session.execute(
        text(
            "SELECT count(*) FROM job WHERE kind = 'gdelt_fetch_article' "
            "AND payload->>'url' LIKE '%rivers-seeks-stronger-trade-ties-with-norway%'"
        )
    ).scalar_one()
    assert queued == 1


def test_an_event_acted_in_niger_is_ignored_even_when_it_is_mentioned(session: Session) -> None:
    add_outlet(session, "outlet", "https://news.example.ng")
    export = "\n".join(
        [
            export_row(EXP_EVENT_ID="1", EXP_ACTION_COUNTRY="NG", EXP_ACTOR1_COUNTRY="NER"),
            export_row(EXP_EVENT_ID="2", EXP_ACTION_COUNTRY="NI"),
        ]
    )
    mentions = "\n".join(
        [
            mention_row(1, "https://news.example.ng/niger-story"),
            mention_row(2, "https://news.example.ng/nigeria-story"),
        ]
    )
    result = gdelt.process_window(session, LATEST, export, mentions)

    assert result.events == 1 and result.new == 1
    assert [r for (r,) in session.execute(select(GdeltDiscovery.mention_identifier))] == [
        "https://news.example.ng/nigeria-story"
    ]
    assert job_count(session, "gdelt_fetch_article") == 1


def test_a_later_mention_of_an_earlier_nigeria_event_is_recorded(session: Session) -> None:
    export = export_row(
        EXP_EVENT_ID="7", EXP_ACTION_COUNTRY="NI", EXP_ACTION_ADM1="NI11", EXP_EVENT_ROOT_CODE="03"
    )
    gdelt.process_window(
        session,
        "20260930174500",
        export,
        mention_row(7, "https://a.example/one", ts="20260930174500"),
    )
    # The next window has no Events row for event 7, only a new article about it, plus a mention of
    # an event nobody has seen as Nigerian.
    later = "\n".join(
        [mention_row(7, "https://b.example/two"), mention_row(8, "https://c.example/three")]
    )
    result = gdelt.process_window(session, LATEST, "", later)

    assert result.new == 1
    row = session.scalars(
        select(GdeltDiscovery).where(GdeltDiscovery.mention_identifier == "https://b.example/two")
    ).one()
    assert (row.action_geo_country, row.action_geo_adm1, row.event_root_code) == (
        "NI",
        "NI11",
        "03",
    )


def test_an_article_already_captured_is_linked_not_fetched_again(
    session: Session, store: S3Store
) -> None:
    source = add_outlet(session, "premiumtimes", "https://www.premiumtimesng.com")
    permission = session.scalars(select(SourcePermission)).one()
    url = "https://www.premiumtimesng.com/business/fuel-price-story.html"
    captured = record_document(
        session, store, source, permission, url=url + "?utm_source=rss", content_type="text/html",
        content=html_page("Petrol price rises"),
    )  # fmt: skip
    export = export_row(EXP_EVENT_ID="3", EXP_ACTION_COUNTRY="NI")
    result = gdelt.process_window(session, LATEST, export, mention_row(3, url))

    assert (result.new, result.linked, result.queued) == (1, 1, 0)
    assert job_count(session, "gdelt_fetch_article") == 0
    row = session.scalars(select(GdeltDiscovery)).one()
    assert row.evidence_document_id == captured.id


# --- fetching an article -------------------------------------------------------------------------


def article_setup(session: Session, **kwargs: Any) -> tuple[Source, str]:
    source = add_outlet(session, "punch", "https://punchng.com", **kwargs)
    return source, "https://punchng.com/2026/09/30/some-story/"


def test_an_on_topic_article_is_captured_under_the_outlet_and_linked(
    session: Session, store: S3Store, net: Network
) -> None:
    source, url = article_setup(session)
    export = export_row(EXP_EVENT_ID="3", EXP_ACTION_COUNTRY="NI")
    gdelt.process_window(session, LATEST, export, mention_row(3, url))
    net.files[url], net.content_types[url] = (
        html_page("Petrol price hits N900 in Lagos"),
        "text/html",
    )

    outcome = gdelt.fetch_article(session, store, url, source.id, fetch=net)

    assert outcome.status == "captured" and outcome.document_id is not None
    document = session.get(EvidenceDocument, outcome.document_id)
    assert document is not None
    assert document.source_id == source.id and document.title == "Petrol price hits N900 in Lagos"
    assert store.exists(document.storage_key)
    assert document.text_content is None  # the permission does not allow storing full text
    assert document.excerpt is not None and len(document.excerpt) <= 300
    assert session.scalars(select(GdeltDiscovery)).one().evidence_document_id == document.id


def test_captured_text_is_stored_only_when_the_permission_allows_it(
    session: Session, store: S3Store, net: Network
) -> None:
    source, url = article_setup(session, full_text=True)
    net.files[url], net.content_types[url] = html_page("Diesel price falls"), "text/html"
    outcome = gdelt.fetch_article(session, store, url, source.id, fetch=net)
    document = session.get(EvidenceDocument, outcome.document_id)
    assert document is not None and document.text_content


def test_an_article_off_topic_is_not_kept(session: Session, store: S3Store, net: Network) -> None:
    source, url = article_setup(session)
    net.files[url], net.content_types[url] = (
        html_page("Super Eagles qualify for the finals"),
        "text/html",
    )
    outcome = gdelt.fetch_article(session, store, url, source.id, fetch=net)
    assert outcome.status == "not_on_topic"
    assert session.scalar(select(func.count()).select_from(EvidenceDocument)) == 0


def test_an_article_already_in_the_evidence_store_is_not_fetched(
    session: Session, store: S3Store, net: Network
) -> None:
    source, url = article_setup(session)
    permission = session.scalars(select(SourcePermission)).one()
    existing = record_document(
        session, store, source, permission, url=url, content_type="text/html",
        content=html_page("Petrol price hits N900"),
    )  # fmt: skip
    outcome = gdelt.fetch_article(session, store, url + "#comments", source.id, fetch=net)
    assert (outcome.status, outcome.document_id) == ("already_captured", existing.id)
    assert net.calls == []


def test_a_page_that_declares_another_canonical_url_already_captured_is_not_stored_twice(
    session: Session, store: S3Store, net: Network
) -> None:
    source, url = article_setup(session)
    permission = session.scalars(select(SourcePermission)).one()
    canonical = "https://punchng.com/2026/09/30/the-real-address/"
    page = (
        f'<html><head><title>Petrol price hits N900</title><link rel="canonical" href="{canonical}">'
        "</head><body><p>text</p></body></html>"
    ).encode()
    existing = record_document(
        session, store, source, permission, url=canonical, content_type="text/html", content=page
    )
    net.files[url], net.content_types[url] = (
        page.replace(b"text</p>", b"text, edited</p>"),
        "text/html",
    )
    outcome = gdelt.fetch_article(session, store, url, source.id, fetch=net)
    assert (outcome.status, outcome.document_id) == ("already_captured", existing.id)


@pytest.mark.parametrize("status", [404, 410])
def test_a_gone_article_is_dropped_quietly(
    session: Session, store: S3Store, net: Network, status: int
) -> None:
    source, url = article_setup(session)
    net.statuses[url] = status
    assert gdelt.fetch_article(session, store, url, source.id, fetch=net).status == "unavailable"


def test_a_server_error_fails_the_job_so_it_is_retried(
    session: Session, store: S3Store, net: Network
) -> None:
    source, url = article_setup(session)
    net.statuses[url] = 503
    with pytest.raises(gdelt.ArticleFetchError):
        gdelt.fetch_article(session, store, url, source.id, fetch=net)


@pytest.mark.parametrize("case", ["unapproved", "inactive"])
def test_a_source_that_may_not_be_collected_is_not_fetched(
    session: Session, store: S3Store, net: Network, case: str
) -> None:
    source, url = article_setup(session, approved=case != "unapproved", active=case != "inactive")
    net.files[url], net.content_types[url] = html_page("Petrol price hits N900"), "text/html"
    assert gdelt.fetch_article(session, store, url, source.id, fetch=net).status == "skipped"
    assert net.calls == []


def test_a_non_html_url_is_not_kept(session: Session, store: S3Store, net: Network) -> None:
    source, url = article_setup(session)
    net.files[url], net.content_types[url] = b"%PDF-1.4 petrol", "application/pdf"
    assert gdelt.fetch_article(session, store, url, source.id, fetch=net).status == "not_html"


# --- polling -------------------------------------------------------------------------------------


def synthetic_window(net: Network, timestamp: str, event_id: int) -> None:
    net.add_window(
        timestamp,
        export_row(EXP_EVENT_ID=str(event_id), EXP_ACTION_COUNTRY="NI"),
        mention_row(event_id, f"https://news.example.ng/story-{event_id}", ts=timestamp),
    )


def last_window(session: Session) -> str | None:
    return gdelt.last_window(session)


def test_the_first_poll_processes_only_the_newest_window_and_checks_the_files(
    session: Session, net: Network
) -> None:
    source = add_gdelt(session)
    net.add_real_latest()
    outcome = gdelt.poll(session, source, fetch=net)

    assert outcome.latest == LATEST and [w.timestamp for w in outcome.windows] == [LATEST]
    assert discoveries(session) == 46
    assert last_window(session) == LATEST
    # lastupdate.txt lists http:// files; every request is https.
    assert net.calls == [
        gdelt.LASTUPDATE_URL,
        f"{gdelt.BASE_URL}/{LATEST}.export.CSV.zip",
        f"{gdelt.BASE_URL}/{LATEST}.mentions.CSV.zip",
    ]


def test_polling_again_with_nothing_new_does_nothing(session: Session, net: Network) -> None:
    source = add_gdelt(session)
    net.add_real_latest()
    gdelt.poll(session, source, fetch=net)
    net.calls.clear()
    outcome = gdelt.poll(session, source, fetch=net)
    assert outcome.windows == [] and outcome.stopped_early is None
    assert net.calls == [gdelt.LASTUPDATE_URL]
    assert discoveries(session) == 46


def test_a_download_that_does_not_match_lastupdate_is_refused(
    session: Session, net: Network
) -> None:
    source = add_gdelt(session)
    net.add_real_latest()
    url = f"{gdelt.BASE_URL}/{LATEST}.export.CSV.zip"
    net.files[url] = net.files[url][:-10] + b"0123456789"  # same size, different bytes
    with pytest.raises(gdelt.GdeltError, match="MD5"):
        gdelt.poll(session, source, fetch=net)
    assert discoveries(session) == 0 and last_window(session) is None


def test_missed_windows_are_replayed_in_order_from_where_it_stopped(
    session: Session, net: Network
) -> None:
    source = add_gdelt(session)
    session.add(Setting(key=gdelt.LAST_WINDOW_SETTING, value="20260930170000"))
    session.flush()
    for i, timestamp in enumerate(["20260930171500", "20260930173000", "20260930174500"], start=1):
        synthetic_window(net, timestamp, i)
    synthetic_window(net, "20260930180000", 4)
    net.set_lastupdate("20260930180000")
    # 17:00 was done; 17:15, 17:30, 17:45 and 18:00 are to do, and 18:00 is the one lastupdate lists.
    outcome = gdelt.poll(session, source, fetch=net)
    assert [w.timestamp for w in outcome.windows] == [
        "20260930171500", "20260930173000", "20260930174500", "20260930180000",
    ]  # fmt: skip
    assert discoveries(session) == 4 and last_window(session) == "20260930180000"


def test_a_long_catch_up_is_spread_over_several_polls(session: Session, net: Network) -> None:
    source = add_gdelt(session)
    session.add(Setting(key=gdelt.LAST_WINDOW_SETTING, value="20260930170000"))
    session.flush()
    stamps = ["20260930171500", "20260930173000", "20260930174500", "20260930180000"]
    for i, timestamp in enumerate(stamps, start=1):
        synthetic_window(net, timestamp, i)
    net.set_lastupdate("20260930180000")

    first = gdelt.poll(session, source, fetch=net, max_windows=3)
    assert len(first.windows) == 3 and last_window(session) == "20260930174500"
    second = gdelt.poll(session, source, fetch=net, max_windows=3)
    assert [w.timestamp for w in second.windows] == ["20260930180000"]
    assert discoveries(session) == 4


def test_a_window_not_published_yet_is_retried_next_time(session: Session, net: Network) -> None:
    source = add_gdelt(session)
    session.add(Setting(key=gdelt.LAST_WINDOW_SETTING, value="20260930171500"))
    session.flush()
    synthetic_window(net, "20260930173000", 1)
    synthetic_window(net, "20260930180000", 3)  # 17:45 is missing, and only 15 minutes behind
    net.set_lastupdate("20260930180000")
    outcome = gdelt.poll(session, source, fetch=net)
    assert [w.timestamp for w in outcome.windows] == ["20260930173000"]
    assert "20260930174500" in (outcome.stopped_early or "")
    assert last_window(session) == "20260930173000"


def test_a_window_gdelt_never_published_is_skipped_after_an_hour(
    session: Session, net: Network
) -> None:
    source = add_gdelt(session)
    session.add(Setting(key=gdelt.LAST_WINDOW_SETTING, value="20260930161500"))
    session.flush()
    for i, timestamp in enumerate(["20260930173000", "20260930174500", "20260930180000"], start=1):
        synthetic_window(net, timestamp, i)
    net.set_lastupdate("20260930180000")
    # 16:30, 16:45 and 17:00 are an hour or more behind the newest window and were never
    # published: skipped. 17:15 is only 45 minutes behind, so it may still appear: the poll waits.
    outcome = gdelt.poll(session, source, fetch=net, max_windows=6)
    assert outcome.skipped == ["20260930163000", "20260930164500", "20260930170000"]
    assert outcome.windows == []
    assert outcome.stopped_early is not None and "20260930171500" in outcome.stopped_early
    assert last_window(session) == "20260930170000"


def test_replaying_a_window_from_the_start_adds_nothing(session: Session, net: Network) -> None:
    source = add_gdelt(session)
    net.add_real_latest()
    gdelt.poll(session, source, fetch=net)
    row = session.get(Setting, gdelt.LAST_WINDOW_SETTING)
    assert row is not None
    row.value = "20260930174500"  # pretend the last poll did not finish
    session.flush()
    outcome = gdelt.poll(session, source, fetch=net)
    assert [w.new for w in outcome.windows] == [0]  # 17:45 is gone, 18:00 replays: nothing new
    assert discoveries(session) == 46


def test_when_the_listing_cannot_be_read_the_poll_fails(session: Session, net: Network) -> None:
    source = add_gdelt(session)
    net.statuses[gdelt.LASTUPDATE_URL] = 503
    with pytest.raises(gdelt.GdeltError):
        gdelt.poll(session, source, fetch=net)


# --- through the job queue -----------------------------------------------------------------------


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield sessionmaker(bind=engine, expire_on_commit=False)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture
def extracted() -> list[int]:
    return []


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch, net: Network, store: S3Store, extracted: list[int]
) -> None:
    def extract_claims(ctx: JobContext) -> None:  # the real one needs a language model
        extracted.append(ctx.job.payload["document_id"])

    monkeypatch.setitem(handlers.HANDLERS, "extract_claims", extract_claims)
    monkeypatch.setitem(handlers.HANDLERS, "gdelt_poll", poll_handler.gdelt_poll)
    monkeypatch.setitem(
        handlers.HANDLERS, "gdelt_fetch_article", fetch_article_handler.gdelt_fetch_article
    )
    monkeypatch.setattr(gdelt, "fetch_document", net)
    monkeypatch.setattr(fetch_article_handler, "get_store", lambda: store)


def drain(factory: sessionmaker[Session]) -> None:
    worker = Worker(factory, "test-worker")
    for _ in range(200):
        if not worker.run_once():
            return


def states(factory: sessionmaker[Session]) -> dict[tuple[str, str], int]:
    with factory() as s:
        rows = s.execute(text("SELECT kind, status, count(*) FROM job GROUP BY 1, 2")).all()
    return {(r[0], r[1]): r[2] for r in rows}


@pytest.mark.usefixtures("wired")
def test_the_poll_job_discovers_then_the_article_job_captures(
    factory: sessionmaker[Session], net: Network, extracted: list[int]
) -> None:
    with factory() as s:
        add_gdelt(s)
        add_outlet(s, "premiumtimes", "https://www.premiumtimesng.com")
        s.commit()
    net.add_real_latest()
    # The fixture's only premiumtimesng.com article; give it a fuel headline.
    article = (
        "https://www.premiumtimesng.com/promoted/913606-nigeria-66-noa-calls-for-citizens-"
        "support-to-sustain-national-unity-and-progress.html"
    )
    net.files[article], net.content_types[article] = (
        html_page("Petrol price: NOA urges calm as pump price rises"),
        "text/html",
    )
    with factory() as s:
        queue.enqueue(s, "gdelt_poll", {}, dedupe_key="gdelt_poll:1")
        s.commit()
    drain(factory)

    with factory() as s:
        source = s.scalars(select(Source).where(Source.slug == "gdelt")).one()
        assert source.health == "healthy" and source.last_success_at is not None
        assert s.scalar(select(func.count()).select_from(GdeltDiscovery)) == 46
        document = s.scalars(select(EvidenceDocument)).one()
        assert document.url == article and document.title.startswith("Petrol price")
        linked = s.scalars(
            select(GdeltDiscovery.evidence_document_id).where(
                GdeltDiscovery.mention_identifier == article
            )
        ).all()
        assert len(linked) >= 2 and set(linked) == {document.id}  # every event row is linked
        assert document.origin_id is not None  # the article job gives it a reporting origin
        assert extracted == [document.id]  # and queues claim extraction, once
    # The fixture has a second premiumtimesng.com URL; the fake network answers 404 for it, which
    # ends that job quietly.
    assert states(factory) == {
        ("gdelt_poll", "done"): 1,
        ("gdelt_fetch_article", "done"): 2,
        ("extract_claims", "done"): 1,
    }


@pytest.mark.usefixtures("wired")
def test_the_poll_job_does_nothing_until_the_source_is_approved(
    factory: sessionmaker[Session], net: Network
) -> None:
    with factory() as s:
        add_gdelt(s, approved=False)
        s.commit()
    net.add_real_latest()
    with factory() as s:
        queue.enqueue(s, "gdelt_poll", {}, dedupe_key="gdelt_poll:1")
        s.commit()
    drain(factory)
    assert net.calls == []
    assert states(factory) == {("gdelt_poll", "done"): 1}


@pytest.mark.usefixtures("wired")
def test_a_failing_poll_marks_the_source_and_is_retried(
    factory: sessionmaker[Session], net: Network
) -> None:
    with factory() as s:
        add_gdelt(s)
        s.commit()
    net.statuses[gdelt.LASTUPDATE_URL] = 503
    with factory() as s:
        queue.enqueue(s, "gdelt_poll", {}, dedupe_key="gdelt_poll:1")
        s.commit()
    Worker(factory, "test-worker").run_once()
    with factory() as s:
        source = s.scalars(select(Source)).one()
        assert source.consecutive_failures == 1 and "503" in (source.last_error or "")
    assert states(factory) == {("gdelt_poll", "queued"): 1}


@pytest.mark.usefixtures("wired")
def test_the_article_job_captures_an_on_topic_page(
    factory: sessionmaker[Session], net: Network, extracted: list[int]
) -> None:
    with factory() as s:
        outlet = add_outlet(s, "punch", "https://punchng.com")
        s.commit()
        outlet_id = outlet.id
    url = "https://punchng.com/2026/09/30/fuel-story/"
    net.files[url], net.content_types[url] = (
        html_page("Fuel price: NNPC raises pump price"),
        "text/html",
    )
    with factory() as s:
        queue.enqueue(s, "gdelt_fetch_article", {"url": url, "source_id": outlet_id})
        s.commit()
    drain(factory)
    with factory() as s:
        document = s.scalars(select(EvidenceDocument)).one()
        assert document.canonical_url == url.rstrip("/") and document.source_id == outlet_id
        assert extracted == [document.id]
    assert states(factory) == {("gdelt_fetch_article", "done"): 1, ("extract_claims", "done"): 1}
