"""NBS end to end through the job queue (AS-010): fetch_source -> process_document -> measurements,
and the operator-upload fallback ``import_nbs_file``. Real worker, real PostgreSQL, committed data.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from africasignal.evidence.capture import capture
from africasignal.jobs import handlers, queue
from africasignal.jobs.handlers import assess_situation as assess_situation_module
from africasignal.jobs.handlers import fetch_source as fetch_source_module
from africasignal.jobs.handlers import import_nbs_file as import_nbs_file_module
from africasignal.jobs.handlers import process_document as process_document_module
from africasignal.jobs.worker import Worker
from africasignal.models import (
    AssessmentVersion,
    EvidenceDocument,
    Measurement,
    Place,
    Series,
    Situation,
    Source,
    SourcePermission,
)
from africasignal.net.fetch import FetchResult
from africasignal.sources import base, nbs
from africasignal.sources.nbs import NbsAdapter
from africasignal.storage import S3Store
from tests.integration.nbs_support import add_places, make_store, release
from tests.integration.test_nbs_adapter import FakeSite
from tests.unit.sources.nbs_fixtures import read

TABLES = (
    "measurement_review, measurement, series, evidence_document, reporting_origin, "
    "place_alias, place, source_permission, source, job"
)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class Site(FakeSite):
    """The fake listing site, which also serves the real workbooks."""

    def __init__(self) -> None:
        super().__init__()
        self.files = {f["source_url"]: f["file"] for f in __import__("json").loads(
            (__import__("pathlib").Path(__file__).parents[1] / "fixtures/nbs/manifest.json").read_text()
        )["files"]}  # fmt: skip
        self.broken: set[str] = set()  # workbook URLs that serve a maintenance page

    def __call__(self, url: str, **kwargs: Any) -> FetchResult:
        if url in self.files:
            self.calls.append((url, kwargs))
            body = (
                b"<html>Down for maintenance</html>"
                if url in self.broken
                else read(self.files[url])
            )
            return FetchResult(
                url=url, status_code=200, headers={"content-type": XLSX}, content=body
            )
        return super().__call__(url, **kwargs)


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
def site() -> Site:
    return Site()


@pytest.fixture(autouse=True)
def wired(monkeypatch: pytest.MonkeyPatch, store: S3Store, site: Site) -> None:
    """Real handlers and worker; the network is the fake site and storage is in memory."""
    for kind, fn in (
        ("fetch_source", fetch_source_module.fetch_source),
        ("process_document", process_document_module.process_document),
        ("assess_situation", assess_situation_module.assess_situation),
        ("import_nbs_file", import_nbs_file_module.import_nbs_file),
    ):
        monkeypatch.setitem(handlers.HANDLERS, kind, fn)
    monkeypatch.setattr(base, "ADAPTERS", {"nbs": NbsAdapter(fetch=site)})
    for module in (fetch_source_module, process_document_module):
        monkeypatch.setattr(module, "get_adapter", lambda name: base.ADAPTERS.get(name))
        monkeypatch.setattr(module, "get_store", lambda: store)
    monkeypatch.setattr(import_nbs_file_module, "get_store", lambda: store)
    monkeypatch.setattr(process_document_module, "capture", functools.partial(capture, fetch=site))
    monkeypatch.setattr(nbs, "RECENT_MONTHS", 2)  # September and October; no August workbooks


def _setup(factory: sessionmaker[Session], *, approved: bool = True) -> int:
    with factory() as s:
        add_places(s)
        source = Source(
            slug="nbs-elibrary", name="NBS", kind="official_statistics", adapter="nbs",
            home_url="https://nigerianstat.gov.ng/elibrary", schedule_minutes=1440,
            max_requests_per_hour=30,
        )  # fmt: skip
        s.add(source)
        s.flush()
        s.add(
            SourcePermission(
                source_id=source.id,
                version=1,
                may_collect=True,
                may_store_full_text=True,
                may_republish_numbers=True,
                approved_at=datetime.now(UTC) if approved else None,
            )  # fmt: skip
        )
        s.commit()
        return source.id


def _drain(factory: sessionmaker[Session], limit: int = 600) -> int:
    worker = Worker(factory, "test-worker")
    ran = 0
    while ran < limit and worker.run_once():
        ran += 1
    return ran


def _enqueue(factory: sessionmaker[Session], kind: str, payload: dict[str, Any]) -> None:
    with factory() as s:
        queue.enqueue(s, kind, payload)
        s.commit()


def _count(factory: sessionmaker[Session], model: type) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


def _source(factory: sessionmaker[Session], source_id: int) -> Source:
    with factory() as s:
        src = s.get(Source, source_id)
        assert src is not None
        return src


def _job_states(factory: sessionmaker[Session]) -> dict[tuple[str, str], int]:
    with factory() as s:
        rows = s.execute(text("SELECT kind, status, count(*) FROM job GROUP BY 1, 2")).all()
    return {(r[0], r[1]): r[2] for r in rows}


def test_fetching_the_source_imports_every_release_in_the_window_and_then_goes_quiet(
    factory: sessionmaker[Session], site: Site
) -> None:
    source_id = _setup(factory)
    _enqueue(factory, "fetch_source", {"source_id": source_id})
    _drain(factory)

    # Each of the 10 documents queues an assessment for every situation it touched: two jobs for
    # each of the 5 x 38 petrol/diesel/kerosene/gas situations and the 4 national food ones. The
    # second job for a situation finds the inputs unchanged.
    assert _job_states(factory) == {
        ("fetch_source", "done"): 1,
        ("process_document", "done"): 10,
        ("assess_situation", "done"): 2 * (5 * 38 + 4),
    }
    # Five state-table items (190 rows each: September, October, October 2023 for 38 places) and
    # four food items (September 3 values, October adds 2 new).
    assert _count(factory, Measurement) == 5 * 190 + 4 * 5
    assert _count(factory, EvidenceDocument) == 10
    assert _count(factory, Situation) == 5 * 38 + 4
    with factory() as s:
        versions = s.scalars(select(AssessmentVersion)).all()
    assert len(versions) == 5 * 38 + 4  # one per situation: the repeat jobs changed nothing
    # The data is from 2024; the worker runs on the real clock, so every assessment is
    # "insufficient evidence", which the publication policy (R3) publishes as a dated card.
    assert {(v.status, v.evidence_state, v.severity) for v in versions} == {
        ("published", "insufficient", "none")
    }
    with factory() as s:
        current = s.scalars(select(Situation.current_version_id)).all()
    assert all(c is not None for c in current) and len(current) == 5 * 38 + 4
    src = _source(factory, source_id)
    assert src.health == "healthy" and src.last_success_at is not None and src.last_error is None
    with factory() as s:
        docs = s.scalars(select(EvidenceDocument).order_by(EvidenceDocument.title)).all()
        assert all(d.title and d.published_at and d.origin_id for d in docs)
        petrol = next(d for d in docs if "Premium Motor Spirit" in d.title and "October" in d.title)
        assert petrol.url == release("PMS_OCT_2024_REPORT.xlsx")["source_url"]
        assert petrol.published_at == datetime(2024, 11, 19, tzinfo=UTC)
        assert petrol.mime == XLSX

    # Run it again: every title is known, so no report pages are fetched and nothing new happens.
    fetched = len(site.calls)
    _enqueue(factory, "fetch_source", {"source_id": source_id})
    _drain(factory)
    assert len(site.calls) == fetched + 1  # the listing only
    assert _count(factory, Measurement) == 5 * 190 + 4 * 5
    assert _job_states(factory)[("process_document", "done")] == 10


def test_a_workbook_that_cannot_be_read_degrades_the_source_after_two_failures(
    factory: sessionmaker[Session], site: Site
) -> None:
    source_id = _setup(factory)
    site.broken.add(release("PMS_OCT_2024_REPORT.xlsx")["source_url"])
    _enqueue(factory, "fetch_source", {"source_id": source_id})
    _drain(factory)  # the broken document fails once and is requeued with a backoff
    src = _source(factory, source_id)
    assert src.consecutive_failures == 1 and src.health == "healthy"
    assert "not a readable Excel workbook" in (src.last_error or "")

    with factory() as s:  # skip the backoff
        s.execute(text("UPDATE job SET run_at = now() WHERE status = 'queued'"))
        s.commit()
    _drain(factory)
    src = _source(factory, source_id)
    assert src.consecutive_failures == 2 and src.health == "degraded"
    # The nine good releases were imported regardless, and nothing was half-imported for the bad one.
    # A failed job rolls back completely, including the evidence row of the bad file; it is
    # captured again on the next attempt.
    assert _count(factory, EvidenceDocument) == 9
    with factory() as s:
        petrol = s.scalars(
            select(Series).where(Series.item_code == "pms_litre")
        ).one()  # from the September release only
        rows = s.scalar(
            select(func.count()).select_from(Measurement).where(Measurement.series_id == petrol.id)
        )
        assert rows == 114


def test_the_operator_upload_fallback_runs_the_same_parser_and_records_the_original_url(
    factory: sessionmaker[Session], store: S3Store
) -> None:
    source_id = _setup(factory)
    info = release("PMS_OCT_2024_REPORT.xlsx")
    store.put("uploads/operator-1.xlsx", read("PMS_OCT_2024_REPORT.xlsx"), XLSX)
    payload = {
        "source_id": source_id,
        "storage_key": "uploads/operator-1.xlsx",
        "original_url": info["source_url"],
        "publication": "pms",
        "published_on": "2024-11-19",
        "title": info["listing_title"],
    }
    _enqueue(factory, "import_nbs_file", payload)
    _drain(factory)

    assert _job_states(factory) == {
        ("import_nbs_file", "done"): 1,
        ("discard_import_upload", "done"): 1,
        ("assess_situation", "done"): 38,  # the country and 37 states
    }
    assert _count(factory, Measurement) == 114
    with factory() as s:
        doc = s.scalars(select(EvidenceDocument)).one()
        assert doc.url == info["source_url"] and doc.title == info["listing_title"]
        assert doc.mime == XLSX and store.exists(doc.storage_key)
        assert not store.exists("uploads/operator-1.xlsx")  # the temporary upload is removed

    # Uploading the same file again changes nothing.
    store.put("uploads/operator-2.xlsx", read("PMS_OCT_2024_REPORT.xlsx"), XLSX)
    _enqueue(factory, "import_nbs_file", {**payload, "storage_key": "uploads/operator-2.xlsx"})
    _drain(factory)
    assert _count(factory, Measurement) == 114 and _count(factory, EvidenceDocument) == 1


def test_an_upload_for_the_wrong_month_is_refused_and_stores_no_measurements(
    factory: sessionmaker[Session], store: S3Store
) -> None:
    source_id = _setup(factory)
    info = release("PMS_OCT_2024_REPORT.xlsx")
    store.put("uploads/wrong.xlsx", read("PMS_OCT_2024_REPORT.xlsx"), XLSX)
    _enqueue(
        factory,
        "import_nbs_file",
        {
            "source_id": source_id, "storage_key": "uploads/wrong.xlsx",
            "original_url": info["source_url"], "publication": "pms",
            "published_on": "2024-10-17",
            "title": "Premium Motor Spirit (Petrol) Price Watch (September 2024)",
        },
    )  # fmt: skip
    _drain(factory, limit=1)
    assert _count(factory, Measurement) == 0
    with factory() as s:
        error = s.execute(text("SELECT last_error FROM job")).scalar_one()
    assert "reference month is October 2024 but the release is for September 2024" in error
    assert store.exists("uploads/wrong.xlsx")  # kept so the operator can retry or download it


def _wrong_month_upload(factory: sessionmaker[Session], store: S3Store, key: str) -> None:
    source_id = _setup(factory)
    info = release("PMS_OCT_2024_REPORT.xlsx")
    store.put(key, read("PMS_OCT_2024_REPORT.xlsx"), XLSX)
    payload = {
        "source_id": source_id, "storage_key": key,
        "original_url": info["source_url"], "publication": "pms",
        "published_on": "2024-10-17",
        "title": "Premium Motor Spirit (Petrol) Price Watch (September 2024)",
    }  # fmt: skip
    with factory() as s:
        queue.enqueue(s, "import_nbs_file", payload, max_attempts=2)  # as the console does
        s.commit()


def _run_due(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        s.execute(text("UPDATE job SET run_at = now() WHERE status = 'queued'"))
        s.commit()
    _drain(factory, limit=1)


def test_a_dead_import_job_removes_its_upload_but_a_retryable_failure_keeps_it(
    factory: sessionmaker[Session], store: S3Store
) -> None:
    _wrong_month_upload(factory, store, "uploads/dies.xlsx")
    _run_due(factory)
    assert _job_states(factory) == {("import_nbs_file", "queued"): 1}
    assert store.exists("uploads/dies.xlsx")  # the next attempt still needs it

    _run_due(factory)
    assert _job_states(factory) == {("import_nbs_file", "dead"): 1}
    assert not store.exists("uploads/dies.xlsx")


def test_a_failing_cleanup_does_not_hide_the_dead_job(
    factory: sessionmaker[Session], store: S3Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> S3Store:
        raise OSError("storage is down")

    _wrong_month_upload(factory, store, "uploads/stuck.xlsx")
    _run_due(factory)
    monkeypatch.setattr(import_nbs_file_module, "get_store", broken)
    # The handler cannot reach storage either, so this attempt fails and the job dies.
    _run_due(factory)
    assert _job_states(factory) == {("import_nbs_file", "dead"): 1}
    assert store.exists("uploads/stuck.xlsx")


def test_the_dead_job_cleanup_only_deletes_files_under_uploads(
    store: S3Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put("evidence/ab/cd.xlsx", b"evidence", XLSX)
    monkeypatch.setattr(import_nbs_file_module, "get_store", lambda: store)
    import_nbs_file_module.discard_upload(
        queue.ClaimedJob(1, "import_nbs_file", {"storage_key": "evidence/ab/cd.xlsx"}, 2, 2)
    )
    assert store.exists("evidence/ab/cd.xlsx")


def test_an_upload_for_a_source_without_approved_permission_is_refused(
    factory: sessionmaker[Session], store: S3Store
) -> None:
    source_id = _setup(factory, approved=False)
    info = release("PMS_OCT_2024_REPORT.xlsx")
    store.put("uploads/x.xlsx", read("PMS_OCT_2024_REPORT.xlsx"), XLSX)
    _enqueue(
        factory,
        "import_nbs_file",
        {
            "source_id": source_id, "storage_key": "uploads/x.xlsx",
            "original_url": info["source_url"], "publication": "pms",
            "published_on": "2024-11-19", "title": info["listing_title"],
        },
    )  # fmt: skip
    _drain(factory, limit=1)
    assert _count(factory, Measurement) == 0 and _count(factory, EvidenceDocument) == 0
    with factory() as s:
        assert (
            "no approved permission" in s.execute(text("SELECT last_error FROM job")).scalar_one()
        )


def test_places_loaded_for_these_tests_include_the_country_and_all_states(
    factory: sessionmaker[Session],
) -> None:
    _setup(factory)
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(Place)) == 38
