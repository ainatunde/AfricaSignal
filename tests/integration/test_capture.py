from collections.abc import Iterator
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal.evidence.capture import (
    DEFAULT_EXCERPT_CHARS,
    CaptureError,
    SourceNotApproved,
    capture,
    load_text,
)
from africasignal.models import EvidenceDocument, Source, SourcePermission
from africasignal.net.fetch import FetchResult
from africasignal.storage import S3Store, evidence_key
from tests.unit.evidence.test_text import HTML, make_pdf

BUCKET = "africasignal-test"
HEADLINE = "Petrol price rises"


@pytest.fixture
def store() -> Iterator[S3Store]:
    """An in-memory S3 (moto) standing in for MinIO."""
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        s3 = S3Store(client, BUCKET)
        client.create_bucket(Bucket=BUCKET)
        yield s3


class FakeFetcher:
    """Returns canned pages and records the arguments it was called with."""

    def __init__(self) -> None:
        self.pages: dict[str, FetchResult] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def page(self, url: str, content: bytes, content_type: str = "text/html") -> None:
        self.pages[url] = FetchResult(
            url=url, status_code=200, headers={"content-type": content_type}, content=content
        )

    def __call__(self, url: str, **kwargs: object) -> FetchResult:
        self.calls.append((url, kwargs))
        return self.pages.get(url, FetchResult(url=url, status_code=404))


def _source(session: Session, slug: str, kind: str = "news_outlet") -> Source:
    source = Source(
        slug=slug, name=slug, kind=kind, adapter="rss", schedule_minutes=30,
        max_requests_per_hour=12,
    )  # fmt: skip
    session.add(source)
    session.flush()
    return source


def _permission(session: Session, source: Source, **overrides: object) -> SourcePermission:
    fields: dict[str, object] = {
        "version": 1,
        "may_collect": True,
        "may_store_full_text": False,
        "max_quote_chars": 300,
        "may_republish_numbers": False,
        "approved_at": datetime.now(UTC),
    }
    fields.update(overrides)
    permission = SourcePermission(source_id=source.id, **fields)
    session.add(permission)
    session.flush()
    return permission


@pytest.fixture
def news(session: Session) -> Source:
    source = _source(session, "punch-rss")
    _permission(session, source)
    return source


@pytest.fixture
def official(session: Session) -> Source:
    source = _source(session, "nbs-elibrary", kind="official_statistics")
    _permission(
        session, source, may_store_full_text=True, max_quote_chars=None, may_republish_numbers=True
    )
    return source


def _rows(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(EvidenceDocument)) or 0


def test_capture_stores_raw_bytes_and_a_row(session: Session, store: S3Store, news: Source) -> None:
    fetcher = FakeFetcher()
    fetcher.page("https://punch.example/a", HTML.encode())
    doc = capture(session, store, news, "https://punch.example/a", fetch=fetcher)

    assert doc.storage_key == evidence_key(doc.content_sha256, "html")
    assert doc.storage_key.startswith("evidence/") and not doc.storage_key.startswith("/")
    assert store.get(doc.storage_key) == HTML.encode()
    assert doc.mime == "text/html" and doc.status == "active"
    assert doc.canonical_url == "https://punch.example/a"
    assert doc.title == HEADLINE
    assert doc.simhash is not None


def test_fetch_uses_the_sources_request_budget(
    session: Session, store: S3Store, news: Source
) -> None:
    fetcher = FakeFetcher()
    fetcher.page("https://punch.example/a", HTML.encode())
    capture(session, store, news, "https://punch.example/a", fetch=fetcher)
    assert fetcher.calls == [("https://punch.example/a", {"max_requests_per_hour": 12})]


def test_same_url_and_bytes_give_one_row_and_one_object(
    session: Session, store: S3Store, news: Source
) -> None:
    fetcher = FakeFetcher()
    fetcher.page("https://punch.example/a", HTML.encode())
    first = capture(session, store, news, "https://punch.example/a", fetch=fetcher)
    fetcher.page("https://punch.example/a?utm_source=x#c", HTML.encode())
    again = capture(session, store, news, "https://punch.example/a?utm_source=x#c", fetch=fetcher)
    assert again.id == first.id
    assert _rows(session) == 1


def test_changed_bytes_give_a_new_row(session: Session, store: S3Store, news: Source) -> None:
    fetcher = FakeFetcher()
    fetcher.page("https://punch.example/a", HTML.encode())
    first = capture(session, store, news, "https://punch.example/a", fetch=fetcher)
    fetcher.page("https://punch.example/a", HTML.replace("N870", "N905").encode())
    second = capture(session, store, news, "https://punch.example/a", fetch=fetcher)
    assert second.id != first.id and second.content_sha256 != first.content_sha256
    assert second.canonical_url == first.canonical_url
    assert _rows(session) == 2
    assert store.exists(first.storage_key) and store.exists(second.storage_key)


def test_same_bytes_from_two_sources_are_two_rows_sharing_one_object(
    session: Session, store: S3Store, news: Source, official: Source
) -> None:
    fetcher = FakeFetcher()
    fetcher.page("https://x.example/a", HTML.encode())
    a = capture(session, store, news, "https://x.example/a", fetch=fetcher)
    b = capture(session, store, official, "https://x.example/a", fetch=fetcher)
    assert a.id != b.id and a.storage_key == b.storage_key


def test_news_source_never_stores_full_text(session: Session, store: S3Store, news: Source) -> None:
    fetcher = FakeFetcher()
    fetcher.page("https://punch.example/a", HTML.encode())
    doc = capture(session, store, news, "https://punch.example/a", fetch=fetcher)
    assert doc.text_content is None
    assert doc.excerpt is not None and 0 < len(doc.excerpt) <= 300
    # The text can still be recovered transiently from the raw object.
    text = load_text(store, doc)
    assert text is not None and "N870 per litre" in text


def test_excerpt_respects_a_short_quote_limit(session: Session, store: S3Store) -> None:
    source = _source(session, "tight")
    _permission(session, source, max_quote_chars=40)
    fetcher = FakeFetcher()
    fetcher.page("https://t.example/a", HTML.encode())
    doc = capture(session, store, source, "https://t.example/a", fetch=fetcher)
    assert doc.excerpt is not None and len(doc.excerpt) == 40


def test_official_source_stores_text_and_a_default_length_excerpt(
    session: Session, store: S3Store, official: Source
) -> None:
    fetcher = FakeFetcher()
    long_page = HTML.replace(
        "</article>", "<p>" + ("More detail about prices. " * 60) + "</p></article>"
    )
    fetcher.page("https://nbs.example/a", long_page.encode())
    doc = capture(session, store, official, "https://nbs.example/a", fetch=fetcher)
    assert doc.text_content is not None and len(doc.text_content) > DEFAULT_EXCERPT_CHARS
    assert doc.excerpt == doc.text_content[:DEFAULT_EXCERPT_CHARS]
    assert load_text(store, doc) == doc.text_content


def test_pdf_text_is_extracted_and_stored_for_a_regulator(session: Session, store: S3Store) -> None:
    source = _source(session, "nerc", kind="regulator")
    _permission(session, source, may_store_full_text=True, max_quote_chars=None)
    fetcher = FakeFetcher()
    fetcher.page(
        "https://nerc.example/order.pdf", make_pdf(["Band A is N209.50 per kWh"]), "application/pdf"
    )
    doc = capture(session, store, source, "https://nerc.example/order.pdf", fetch=fetcher)
    assert doc.mime == "application/pdf" and doc.storage_key.endswith(".pdf")
    assert doc.text_content is not None and "N209.50 per kWh" in doc.text_content


def test_binary_formats_are_stored_without_text(
    session: Session, store: S3Store, official: Source
) -> None:
    fetcher = FakeFetcher()
    fetcher.page(
        "https://nbs.example/pms.xlsx",
        b"PK\x03\x04data",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    doc = capture(session, store, official, "https://nbs.example/pms.xlsx", fetch=fetcher)
    assert doc.storage_key.endswith(".xlsx") and doc.text_content is None and doc.excerpt is None
    assert doc.simhash is None


def test_declared_canonical_url_is_used(session: Session, store: S3Store, news: Source) -> None:
    html = HTML.replace(
        "<head>", '<head><link rel="canonical" href="https://punch.example/news/petrol">'
    )
    fetcher = FakeFetcher()
    fetcher.page("https://punch.example/amp/petrol?utm_source=x", html.encode())
    doc = capture(
        session, store, news, "https://punch.example/amp/petrol?utm_source=x", fetch=fetcher
    )
    assert doc.canonical_url == "https://punch.example/news/petrol"
    assert doc.url == "https://punch.example/amp/petrol?utm_source=x"


def test_retention_is_derived_from_the_permission(session: Session, store: S3Store) -> None:
    source = _source(session, "short-lived")
    _permission(session, source, retention_days=30)
    fetcher = FakeFetcher()
    fetcher.page("https://s.example/a", HTML.encode())
    now = datetime(2026, 9, 1, tzinfo=UTC)
    doc = capture(session, store, source, "https://s.example/a", fetch=fetcher, now=now)
    assert doc.retrieved_at == now
    assert doc.retention_until == datetime(2026, 10, 1, tzinfo=UTC)


def test_source_without_approved_permission_is_refused_before_fetching(
    session: Session, store: S3Store
) -> None:
    source = _source(session, "unapproved")
    _permission(session, source, approved_at=None)
    fetcher = FakeFetcher()
    with pytest.raises(SourceNotApproved):
        capture(session, store, source, "https://u.example/a", fetch=fetcher)
    assert fetcher.calls == []


def test_permission_that_forbids_collecting_is_refused(session: Session, store: S3Store) -> None:
    source = _source(session, "forbidden")
    _permission(session, source, may_collect=False)
    with pytest.raises(SourceNotApproved):
        capture(session, store, source, "https://u.example/a", fetch=FakeFetcher())


def test_newest_approved_permission_version_wins(session: Session, store: S3Store) -> None:
    source = _source(session, "versions")
    _permission(session, source, version=1, may_store_full_text=True, max_quote_chars=None)
    _permission(session, source, version=2, may_store_full_text=False)
    _permission(session, source, version=3, approved_at=None, may_store_full_text=True)  # draft
    fetcher = FakeFetcher()
    fetcher.page("https://v.example/a", HTML.encode())
    doc = capture(session, store, source, "https://v.example/a", fetch=fetcher)
    assert doc.text_content is None  # version 2 is in force


def test_failed_fetch_raises_and_stores_nothing(
    session: Session, store: S3Store, news: Source
) -> None:
    fetcher = FakeFetcher()  # every URL is a 404
    with pytest.raises(CaptureError, match="404"):
        capture(session, store, news, "https://punch.example/missing", fetch=fetcher)
    assert _rows(session) == 0


def test_object_store_rejects_unsafe_keys(store: S3Store) -> None:
    for key in ("/etc/passwd", "evidence/../x", "", "a\\b"):
        with pytest.raises(ValueError):
            store.put(key, b"x")


def test_object_store_round_trip_and_delete(store: S3Store) -> None:
    key = evidence_key("ab" * 32, "html")
    assert key == f"evidence/ab/ab/{'ab' * 32}.html"
    assert store.exists(key) is False
    store.put(key, b"hello", "text/html")
    assert store.exists(key) and store.get(key) == b"hello"
    store.delete(key)
    assert store.exists(key) is False


def test_evidence_key_validates_input() -> None:
    for bad_sha in ("short", "G" * 64, "A" * 64):
        with pytest.raises(ValueError):
            evidence_key(bad_sha, "html")
    with pytest.raises(ValueError):
        evidence_key("a" * 64, "../x")
