"""Console pages for operations: jobs, assessments, range checks, costs, NBS upload, discovered
domains, channel posts and backup alerts. Every change is audited and admin-only."""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from africasignal import backup_alerts, settings_store
from africasignal.jobs import queue
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.handlers.import_nbs_file import import_nbs_file
from africasignal.jobs.queue import ClaimedJob
from africasignal.models import (
    AssessmentVersion,
    AuditLog,
    DiscoveredDomainDecision,
    GdeltDiscovery,
    Job,
    LlmCall,
    Measurement,
    MeasurementReview,
    Outbox,
    Place,
    Series,
    Situation,
    Source,
    SourcePermission,
)
from africasignal.publish.situations import assess_situation, ensure_situations
from africasignal.publish.versions import apply_policy, set_publication_suspended
from africasignal.storage import S3Store
from africasignal.web.routes import admin_ops
from tests.integration.email_support import add_place, add_situation, add_version
from tests.integration.nbs_support import add_places, add_source, import_bytes, make_store
from tests.integration.test_admin_console import (
    ORIGIN,
    client,  # noqa: F401  (fixture)
    make_operator,
    make_source,
    signed_in,
)
from tests.unit.sources.nbs_fixtures import edit, read

PMS_SEP, PMS_OCT = "FUEL_SEPT_2024_REPORT.xlsx", "PMS_OCT_2024_REPORT.xlsx"
WHEN = datetime(2024, 11, 25, 12, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from make_store()


@pytest.fixture
def admin(client: TestClient, session: Session) -> TestClient:  # noqa: F811
    return signed_in(client, make_operator(session))


def actions(session: Session) -> list[str]:
    return list(session.scalars(select(AuditLog.action).order_by(AuditLog.id)))


def post(admin: TestClient, path: str, **form: str):  # type: ignore[no-untyped-def]
    return admin.post(path, data=form, headers=ORIGIN)


PAGES = (
    "/admin/jobs",
    "/admin/assessments",
    "/admin/range-checks",
    "/admin/costs",
    "/admin/nbs-upload",
    "/admin/domains",
    "/admin/channel-posts",
    "/admin/alerts",
)


# --- access -------------------------------------------------------------------------------------


def test_every_page_needs_sign_in(client: TestClient) -> None:  # noqa: F811
    for path in PAGES:
        response = client.get(path)
        assert (response.status_code, response.headers["location"]) == (303, "/admin/login"), path


def test_editors_cannot_open_or_use_the_pages(client: TestClient, session: Session) -> None:  # noqa: F811
    signed_in(client, make_operator(session, "editor@example.org", "editor"))
    for path in PAGES:
        assert client.get(path).status_code == 403, path
    assert post(client, "/admin/jobs/1/retry").status_code == 403
    assert post(client, "/admin/domains/reject", domain="a.example").status_code == 403
    assert actions(session) == ["operator.sign_in"]


def test_an_editor_who_types_an_admin_address_gets_a_console_page_not_json(
    client: TestClient,  # noqa: F811
    session: Session,
) -> None:
    signed_in(client, make_operator(session, "editor@example.org", "editor"))
    response = client.get("/admin/jobs")
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "This page is for admin operators" in response.text
    assert '"detail"' not in response.text and 'href="/admin/sources"' in response.text
    assert response.headers["cache-control"] == "no-store"  # still a console response
    missing = client.get("/admin/sources/99999")
    assert missing.status_code == 404 and "There is no such console page." in missing.text
    assert "AfricaSignal console" in missing.text  # not the public site's 404 page


def test_every_page_renders_empty_for_an_admin(admin: TestClient) -> None:
    for path in PAGES:
        response = admin.get(path)
        assert response.status_code in (200, 400), path  # channel posts: no public address yet
        assert "Console" in response.text or "console" in response.text


def test_the_navigation_links_every_page(admin: TestClient) -> None:
    page = admin.get("/admin/jobs").text
    for path in PAGES:
        assert f'href="{path}"' in page


def test_state_changes_from_another_origin_are_refused(admin: TestClient) -> None:
    response = admin.post("/admin/jobs/1/retry", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


# --- jobs ---------------------------------------------------------------------------------------


def make_job(session: Session, status: str, kind: str = "fetch_source", error: str = "boom") -> Job:
    job = Job(kind=kind, payload={"secret": "x"}, status=status, attempts=5, last_error=error)
    session.add(job)
    session.flush()
    return job


def test_jobs_page_counts_by_kind_and_lists_dead_jobs(admin: TestClient, session: Session) -> None:
    make_job(session, "dead", error="HTTP 500 from the source")
    make_job(session, "dead", "extract_claims")
    make_job(session, "done")
    make_job(session, "queued")
    page = admin.get("/admin/jobs").text
    assert "HTTP 500 from the source" in page
    assert "fetch_source" in page and "extract_claims" in page
    assert "secret" not in page  # payloads are not shown


def test_retrying_a_dead_job_requeues_it_with_fresh_attempts_and_is_audited(
    admin: TestClient, session: Session
) -> None:
    job = make_job(session, "dead")
    response = post(admin, f"/admin/jobs/{job.id}/retry")
    assert (response.status_code, response.headers["location"]) == (
        303,
        "/admin/jobs?notice=job_retried",
    )
    session.refresh(job)
    assert (job.status, job.attempts, job.finished_at, job.locked_by) == ("queued", 0, None, None)
    row = session.scalars(select(AuditLog).where(AuditLog.action == "job.retry")).one()
    assert (row.target_id, row.after["status"]) == (job.id, "queued")  # type: ignore[index]
    # The queue can claim it again.
    claimed = queue.claim(session, "w1")
    assert claimed is not None and claimed.id == job.id


def test_only_dead_or_failed_jobs_can_be_retried(admin: TestClient, session: Session) -> None:
    job = make_job(session, "done")
    response = post(admin, f"/admin/jobs/{job.id}/retry")
    assert response.status_code == 400 and "only a dead or failed job" in response.text
    assert post(admin, "/admin/jobs/999999/retry").status_code == 400
    assert "job.retry" not in actions(session)


# --- assessments --------------------------------------------------------------------------------


def held_version(session: Session, store: S3Store, source: Source) -> AssessmentVersion:
    import_bytes(session, store, source, PMS_OCT)
    place_id = session.scalars(select(Place.id).where(Place.code == "NG-LA")).one()
    (situation,) = ensure_situations(session, [("pms_litre", place_id)])
    version = assess_situation(session, situation.id, WHEN).version
    assert version is not None
    decision = apply_policy(session, version.id, WHEN)
    assert decision is not None and decision.status == "held"
    return version


@pytest.fixture
def source(session: Session) -> Source:
    add_places(session)
    return add_source(session)


def test_the_hold_queue_lists_a_held_version_and_it_can_be_withheld(
    admin: TestClient, session: Session, store: S3Store, source: Source
) -> None:
    version = held_version(session, store, source)
    page = admin.get("/admin/assessments").text
    assert "Nothing is waiting" not in page and version.headline in page

    short = post(admin, f"/admin/assessments/versions/{version.id}/withhold", reason="no")
    assert short.status_code == 400 and "at least 8 characters" in short.text
    assert version.status == "draft"

    response = post(
        admin,
        f"/admin/assessments/versions/{version.id}/withhold",
        reason="figure looks wrong against the bulletin",
    )
    assert response.headers["location"] == "/admin/assessments?notice=withheld"
    session.refresh(version)
    assert version.status == "withheld" and version.hold_until is None
    assert version.withheld_reasons == ["operator: figure looks wrong against the bulletin"]
    row = session.scalars(select(AuditLog).where(AuditLog.action == "assessment.withhold")).one()
    assert row.after["reason"] == "figure looks wrong against the bulletin"  # type: ignore[index]
    again = post(admin, f"/admin/assessments/versions/{version.id}/withhold", reason="a second try")
    assert again.status_code == 400 and "not waiting in the review hold" in again.text


def test_a_held_version_can_be_published_early_and_it_becomes_current(
    admin: TestClient, session: Session, store: S3Store, source: Source
) -> None:
    version = held_version(session, store, source)
    response = post(
        admin,
        f"/admin/assessments/versions/{version.id}/release",
        reason="checked against the NBS bulletin",
    )
    assert response.headers["location"] == "/admin/assessments?notice=released"
    session.refresh(version)
    situation = session.get(Situation, version.situation_id)
    assert situation is not None
    assert version.status == "published" and situation.current_version_id == version.id
    assert "assessment.release_early" in actions(session)


def test_publishing_early_cannot_beat_the_publication_policy(
    admin: TestClient, session: Session, store: S3Store, source: Source
) -> None:
    version = held_version(session, store, source)
    set_publication_suspended(session, True, WHEN)  # R1: the kill switch
    response = post(
        admin,
        f"/admin/assessments/versions/{version.id}/release",
        reason="checked against the NBS bulletin",
    )
    assert response.status_code == 200 and "withholds this version now (R1)" in response.text
    session.refresh(version)
    situation = session.get(Situation, version.situation_id)
    assert situation is not None
    assert (version.status, version.withheld_reasons) == ("withheld", ["R1"])
    assert situation.current_version_id is None
    row = session.scalars(
        select(AuditLog).where(AuditLog.action == "assessment.release_early")
    ).one()
    assert row.after["withheld_by"] == ["R1"]  # type: ignore[index]


def test_a_current_situation_can_be_withdrawn_with_a_reason(
    admin: TestClient, session: Session
) -> None:
    place = add_place(session, "NG", "Nigeria", "country")
    situation = add_situation(session, "pms", place)
    current = add_version(session, situation)
    page = admin.get("/admin/assessments").text
    assert f"/admin/assessments/situations/{situation.id}/withdraw" in page

    bad = post(admin, f"/admin/assessments/situations/{situation.id}/withdraw", reason="")
    assert bad.status_code == 400
    response = post(
        admin,
        f"/admin/assessments/situations/{situation.id}/withdraw",
        reason="the NBS file was withdrawn by the publisher",
    )
    assert response.headers["location"] == "/admin/assessments?notice=withdrawn"
    session.refresh(situation)
    new = session.get(AssessmentVersion, situation.current_version_id)
    assert new is not None and new.id != current.id
    assert new.status == "withdrawn"
    assert new.headline == "Withdrawn: the NBS file was withdrawn by the publisher"
    session.refresh(current)
    assert current.status == "superseded"
    row = session.scalars(select(AuditLog).where(AuditLog.action == "assessment.withdraw")).one()
    assert row.target_id == situation.id
    again = post(
        admin, f"/admin/assessments/situations/{situation.id}/withdraw", reason="a second reason"
    )
    assert again.status_code == 400 and "no published version" in again.text


def test_the_status_filter_ignores_unknown_values(admin: TestClient, session: Session) -> None:
    place = add_place(session, "NG", "Nigeria", "country")
    add_version(session, add_situation(session, "pms", place), status="withheld", current=False)
    assert "Price pms" in admin.get("/admin/assessments?status=withheld").text
    assert "Price pms" not in admin.get("/admin/assessments?status=published").text
    assert admin.get("/admin/assessments?status=%27%3B--").status_code == 200


# --- range checks -------------------------------------------------------------------------------


def pending_review(session: Session, store: S3Store, source: Source) -> MeasurementReview:
    import_bytes(session, store, source, PMS_SEP)
    # October's Lagos value has a slipped decimal point: it goes to the review queue.
    import_bytes(
        session, store, source, PMS_OCT, edit(PMS_OCT, "Fuel_October 2024", {"D40": 10809.5})
    )
    return session.scalars(select(MeasurementReview)).one()


def test_the_range_queue_lists_the_value_and_approving_it_stores_a_measurement(
    admin: TestClient, session: Session, store: S3Store, source: Source
) -> None:
    review = pending_review(session, store, source)
    page = admin.get("/admin/range-checks").text
    assert "10809.50" in page and "pms_litre" in page
    before = session.scalar(select(Measurement.id).where(Measurement.value == Decimal("10809.50")))
    assert before is None
    jobs_before = len(list(session.scalars(select(Job.id).where(Job.kind == "assess_situation"))))

    no_note = post(admin, f"/admin/range-checks/{review.id}/approve", note="")
    assert no_note.status_code == 400 and review.status == "pending"
    response = post(
        admin, f"/admin/range-checks/{review.id}/approve", note="matches the printed table"
    )
    assert response.headers["location"] == "/admin/range-checks?notice=range_approved"
    session.refresh(review)
    assert review.status == "approved" and review.resolved_at is not None
    stored = session.scalars(select(Measurement).where(Measurement.value == Decimal("10809.50")))
    assert stored.one().evidence_document_id == review.evidence_document_id
    jobs_after = len(list(session.scalars(select(Job.id).where(Job.kind == "assess_situation"))))
    assert jobs_after > jobs_before  # the assessment runs again with the value
    row = session.scalars(
        select(AuditLog).where(AuditLog.action == "measurement_review.approve")
    ).one()
    assert row.after["note"] == "matches the printed table"  # type: ignore[index]
    again = post(admin, f"/admin/range-checks/{review.id}/approve", note="second approval")
    assert again.status_code == 400 and "already approved" in again.text


def test_rejecting_a_value_stores_nothing_and_queues_the_assessment(
    admin: TestClient, session: Session, store: S3Store, source: Source
) -> None:
    review = pending_review(session, store, source)
    response = post(
        admin, f"/admin/range-checks/{review.id}/reject", note="decimal point slipped in the file"
    )
    assert response.headers["location"] == "/admin/range-checks?notice=range_rejected"
    session.refresh(review)
    assert review.status == "rejected"
    assert (
        session.scalar(select(Measurement.id).where(Measurement.value == Decimal("10809.50")))
        is None
    )
    assert "measurement_review.reject" in actions(session)
    assert (
        post(admin, f"/admin/range-checks/{review.id}/other", note="whatever it is").status_code
        == 404
    )


def test_rejecting_a_value_releases_the_r4_hold(
    admin: TestClient, session: Session, store: S3Store, source: Source
) -> None:
    review = pending_review(session, store, source)
    place_id = review.place_id
    (situation,) = ensure_situations(session, [("pms_litre", place_id)])
    first = assess_situation(session, situation.id, WHEN).version
    assert first is not None
    decision = apply_policy(session, first.id, WHEN)
    assert decision is not None and decision.reasons == ("R4",)
    post(admin, f"/admin/range-checks/{review.id}/reject", note="decimal point slipped in the file")
    assert first.status == "withheld"  # nothing changes the inputs, yet it must be judged again
    # The re-assessment is queued under its own key, not swallowed by the earlier job.
    keys = list(session.scalars(select(Job.dedupe_key).where(Job.kind == "assess_situation")))
    assert f"assess_situation:{situation.id}:review:{review.id}" in keys
    outcome = assess_situation(session, situation.id, WHEN)
    assert outcome.outcome == "created" and outcome.version is not None
    assert outcome.version.id != first.id and outcome.version.supersedes_id == first.id
    decision = apply_policy(session, outcome.version.id, WHEN)
    assert decision is not None and "R4" not in decision.reasons


# --- costs --------------------------------------------------------------------------------------


def test_costs_by_day_and_purpose_and_email_counts(admin: TestClient, session: Session) -> None:
    now = datetime.now(UTC)
    for purpose, cost, cache_hit in (
        ("claim_extract", "0.01200", False),
        ("claim_extract", "0.00800", False),
        ("claim_extract", "0.00000", True),
        ("explain", "0.00500", False),
    ):
        session.add(
            LlmCall(
                purpose=purpose,
                model_id="m",
                prompt_version="v1",
                input_tokens=1000,
                output_tokens=200,
                cost_usd=Decimal(cost),
                cache_hit=cache_hit,
                ts=now,
            )
        )
    session.add(
        LlmCall(
            purpose="old",
            model_id="m",
            prompt_version="v1",
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal("9"),
            ts=now - timedelta(days=60),
        )
    )
    for n, status in enumerate(("sent", "sent", "failed")):
        session.add(Outbox(kind="email_login", payload={}, dedupe_key=f"k{n}", status=status))
    session.flush()
    page = admin.get("/admin/costs").text
    assert "claim_extract" in page and "explain" in page
    assert "0.0200" in page  # two live extraction calls
    assert "0.0250" in page  # the day's total
    assert "old" not in page  # outside the window
    assert "email_login" in page and ">2<" in page
    wider = admin.get("/admin/costs?days=90").text
    assert "old" in wider
    assert admin.get("/admin/costs?days=-5").status_code == 200


def test_costs_shows_todays_budget(admin: TestClient, session: Session) -> None:
    operator = make_operator(session, "other@example.org")
    settings_store.apply_changes(session, operator.operator, {"llm_daily_budget_usd": "2.50"})
    session.add(
        LlmCall(
            purpose="explain",
            model_id="m",
            prompt_version="v1",
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal("2.10"),
        )
    )
    session.flush()
    page = admin.get("/admin/costs").text
    assert "$2.10" in page and "$2.50" in page and "84%" in page


# --- NBS upload ---------------------------------------------------------------------------------


def upload(admin: TestClient, data: bytes, **override: str):  # type: ignore[no-untyped-def]
    form = {
        "source_id": str(override.pop("source_id", "")),
        "publication": "pms",
        "title": "Premium Motor Spirit (Petrol) Price Watch (October 2024)",
        "published_on": "2024-11-19",
        "original_url": "https://nigerianstat.gov.ng/resource/PMS_OCT_2024.xlsx",
        **override,
    }
    return admin.post(
        "/admin/nbs-upload",
        data=form,
        files={"file": ("PMS.xlsx", data, "application/octet-stream")},
        headers=ORIGIN,
    )


@pytest.fixture
def nbs(session: Session, store: S3Store, monkeypatch: pytest.MonkeyPatch) -> Source:
    monkeypatch.setattr(admin_ops, "store_for_session", lambda db: store)
    return make_source(session, "nbs-upload", approved_at=datetime.now(UTC))


def test_the_publication_dropdown_shows_names_and_sends_codes(
    admin: TestClient, nbs: Source
) -> None:
    page = admin.get("/admin/nbs-upload").text
    assert re.search(r'<option value="pms"[^>]*>Petrol \(PMS\) price watch</option>', page)
    for name in (
        "Diesel (AGO) price watch",
        "Household kerosene price watch",
        "Cooking gas (LPG) price watch",
        "Selected food prices watch",
    ):
        assert name in page
    assert ">pms</option>" not in page and ">food</option>" not in page


def test_an_nbs_file_is_stored_under_a_server_chosen_key_and_the_import_is_queued(
    admin: TestClient, session: Session, store: S3Store, nbs: Source
) -> None:
    data = read(PMS_OCT)
    response = upload(admin, data, source_id=str(nbs.id))
    assert response.status_code == 303 and response.headers["location"].startswith("/admin/jobs")
    job = session.scalars(select(Job).where(Job.kind == "import_nbs_file")).one()
    key = job.payload["storage_key"]
    assert key.startswith("uploads/nbs-") and key.endswith(".xlsx")
    assert store.get(key) == data
    assert job.payload["publication"] == "pms"
    assert job.payload["published_on"] == "2024-11-19"
    assert job.payload["original_url"] == "https://nigerianstat.gov.ng/resource/PMS_OCT_2024.xlsx"
    row = session.scalars(select(AuditLog).where(AuditLog.action == "nbs_upload.queue")).one()
    assert row.after["bytes"] == len(data)  # type: ignore[index]


def test_the_import_job_runs_the_same_parser_on_the_uploaded_file(
    admin: TestClient,
    session: Session,
    store: S3Store,
    nbs: Source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload(admin, read(PMS_OCT), source_id=str(nbs.id))
    job = session.scalars(select(Job).where(Job.kind == "import_nbs_file")).one()
    add_places(session)
    from africasignal.jobs.handlers import import_nbs_file as module

    monkeypatch.setattr(module, "get_store", lambda: store)
    import_nbs_file(JobContext(session, ClaimedJob(job.id, job.kind, job.payload, 1, 2), "w1"))
    assert session.scalars(select(Series)).all()
    assert session.scalars(select(Measurement.id)).first() is not None
    assert not store.exists(job.payload["storage_key"])  # the temporary upload is removed


@pytest.mark.parametrize(
    ("data", "override", "message"),
    [
        (b"", {}, "file is empty"),
        (b"just text", {}, "not an Excel"),
        (b"PK\x03\x04 broken", {}, "damaged"),
        (read(PMS_OCT), {"original_url": "https://evil.example/a.xlsx"}, "must be on nigerianstat"),
        (read(PMS_OCT), {"original_url": "file:///etc/passwd"}, "plain http"),
        (read(PMS_OCT), {"publication": "bitcoin"}, "choose a publication"),
        (read(PMS_OCT), {"published_on": "19/11/2024"}, "YYYY-MM-DD"),
        (read(PMS_OCT), {"published_on": "2999-01-01"}, "real past date"),
        (read(PMS_OCT), {"title": ""}, "release title"),
    ],
    ids=[
        "empty",
        "text",
        "broken-zip",
        "foreign-url",
        "file-url",
        "unknown-publication",
        "bad-date",
        "future-date",
        "no-title",
    ],
)
def test_bad_uploads_are_refused_and_nothing_is_stored(
    admin: TestClient,
    session: Session,
    store: S3Store,
    nbs: Source,
    data: bytes,
    override: dict[str, str],
    message: str,
) -> None:
    response = upload(admin, data, source_id=str(nbs.id), **override)
    assert response.status_code == 400 and message in response.text
    assert session.scalars(select(Job).where(Job.kind == "import_nbs_file")).first() is None
    assert "nbs_upload.queue" not in actions(session)
    raw = store._client  # type: ignore[attr-defined]
    assert raw.list_objects_v2(Bucket="africasignal-test").get("KeyCount", 0) == 0


def test_a_zip_without_a_workbook_is_refused(
    admin: TestClient, session: Session, nbs: Source
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("report.pdf", b"%PDF")
    response = upload(admin, buffer.getvalue(), source_id=str(nbs.id))
    assert response.status_code == 400 and "holds no Excel workbook" in response.text


def test_a_zip_that_holds_a_workbook_is_accepted(
    admin: TestClient, session: Session, nbs: Source
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("report.pdf", b"%PDF")
        archive.writestr("table.xlsx", read(PMS_OCT))
    assert upload(admin, buffer.getvalue(), source_id=str(nbs.id)).status_code == 303


def test_a_source_without_approved_permission_or_not_nbs_is_refused(
    admin: TestClient, session: Session, nbs: Source
) -> None:
    session.execute(update(SourcePermission).values(approved_at=None))
    session.flush()
    response = upload(admin, read(PMS_OCT), source_id=str(nbs.id))
    assert response.status_code == 400 and "no approved permission" in response.text
    other = Source(slug="rss-x", name="X", kind="news_outlet", adapter="rss", schedule_minutes=30)
    session.add(other)
    session.flush()
    response = upload(admin, read(PMS_OCT), source_id=str(other.id))
    assert response.status_code == 400 and "choose one of the NBS sources" in response.text
    assert upload(admin, read(PMS_OCT), source_id="abc").status_code == 400


def test_the_upload_needs_object_storage(
    admin: TestClient, session: Session, nbs: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin_ops, "store_for_session", lambda db: None)
    response = upload(admin, read(PMS_OCT), source_id=str(nbs.id))
    assert response.status_code == 400 and "Object storage is not set up" in response.text


def test_the_upload_form_requires_a_file(admin: TestClient, nbs: Source) -> None:
    response = admin.post("/admin/nbs-upload", data={"source_id": str(nbs.id)}, headers=ORIGIN)
    assert response.status_code == 400 and "Choose the file" in response.text


@pytest.mark.parametrize(
    "key", ["evidence/ab/cd/x.html", "uploads/../evidence/x.xlsx", "uploads/a/b.xlsx", "x.xlsx", ""]
)
def test_the_import_job_only_reads_flat_files_under_uploads(
    session: Session, store: S3Store, nbs: Source, key: str
) -> None:
    payload = {
        "source_id": nbs.id,
        "storage_key": key,
        "original_url": "https://nigerianstat.gov.ng/x.xlsx",
        "publication": "pms",
        "published_on": "2024-11-19",
    }
    with pytest.raises(ValueError, match="under uploads"):
        import_nbs_file(JobContext(session, ClaimedJob(1, "import_nbs_file", payload, 1, 2), "w"))


# --- discovered domains -------------------------------------------------------------------------


def discover(session: Session, *urls: str) -> None:
    for n, url in enumerate(urls):
        session.add(
            GdeltDiscovery(
                global_event_id=n + 1,
                mention_identifier=url,
                mention_ts=datetime.now(UTC),
            )
        )
    session.flush()


def test_the_report_lists_unknown_domains_most_linked_first(
    admin: TestClient, session: Session
) -> None:
    discover(
        session,
        "https://www.newsy.example/a",
        "https://newsy.example/b",
        "https://other.example/c",
    )
    page = admin.get("/admin/domains").text
    assert page.index("newsy.example") < page.index("other.example")
    assert "www.newsy.example" not in page


def test_rejecting_a_domain_removes_it_from_the_report_and_is_audited(
    admin: TestClient, session: Session
) -> None:
    discover(session, "https://spam.example/a", "https://news.example/b")
    response = post(admin, "/admin/domains/reject", domain="spam.example", note="content farm")
    assert response.headers["location"] == "/admin/domains?notice=domain_rejected"
    page = admin.get("/admin/domains").text
    assert "news.example" in page and "<td>spam.example</td>" in page  # only under "Decided"
    assert page.count("spam.example") < page.count("news.example") + 5
    row = session.scalars(
        select(DiscoveredDomainDecision).where(DiscoveredDomainDecision.domain == "spam.example")
    ).one()
    assert (row.status, row.note) == ("rejected", "content farm")
    assert "discovered_domain.reject" in actions(session)
    again = post(admin, "/admin/domains/reject", domain="spam.example")
    assert again.status_code == 400 and "already been decided" in again.text
    unknown = post(admin, "/admin/domains/reject", domain="never-seen.example")
    assert unknown.status_code == 400 and "not in the discovered-domains report" in unknown.text
    assert post(admin, "/admin/domains/reject", domain="not a domain").status_code == 400


def test_adding_a_domain_creates_an_inactive_source_with_an_owner_and_no_permission(
    admin: TestClient, session: Session
) -> None:
    discover(session, "https://news.example/a")
    response = post(
        admin,
        "/admin/domains/add",
        domain="news.example",
        name="News Example",
        owner="Example Media Ltd",
        feed_url="https://news.example/feed/",
    )
    assert response.status_code == 303
    source = session.scalars(select(Source).where(Source.slug == "news-example-rss")).one()
    assert response.headers["location"] == f"/admin/sources/{source.id}?notice=domain_added"
    assert (source.kind, source.adapter, source.active, source.owner) == (
        "news_outlet",
        "rss",
        False,
        "Example Media Ltd",
    )
    assert (
        source.home_url == "https://news.example"
        and source.feed_url == "https://news.example/feed/"
    )
    assert not list(
        session.scalars(select(SourcePermission).where(SourcePermission.source_id == source.id))
    )
    decision = session.scalars(select(DiscoveredDomainDecision)).one()
    assert (decision.status, decision.source_id) == ("added", source.id)
    assert "discovered_domain.add_source" in actions(session)
    assert "news.example" not in admin.get("/admin/domains").text.split("Decided")[0]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"owner": ""}, "name the owner"),
        ({"name": ""}, "name (up to"),
        ({"feed_url": "https://elsewhere.example/feed"}, "must be on news.example"),
        ({"feed_url": "ftp://news.example/feed"}, "plain http"),
        ({"feed_url": ""}, "plain http"),
    ],
)
def test_adding_a_domain_needs_an_owner_a_name_and_a_feed_on_the_domain(
    admin: TestClient, session: Session, override: dict[str, str], message: str
) -> None:
    discover(session, "https://news.example/a")
    form = {
        "domain": "news.example",
        "name": "News Example",
        "owner": "Example Media Ltd",
        "feed_url": "https://news.example/feed/",
        **override,
    }
    response = post(admin, "/admin/domains/add", **form)
    assert response.status_code == 400 and message in response.text
    assert session.scalars(select(Source).where(Source.slug.like("news-example%"))).first() is None
    assert session.scalars(select(DiscoveredDomainDecision)).first() is None


def test_an_approved_outlet_no_longer_appears_in_the_report(
    admin: TestClient, session: Session
) -> None:
    discover(session, "https://news.example/a")
    outlet = Source(
        slug="news-ex",
        name="News",
        kind="news_outlet",
        adapter="rss",
        home_url="https://news.example",
        owner="Example Media Ltd",
        schedule_minutes=30,
        active=True,
    )
    session.add(outlet)
    session.flush()
    session.add(
        SourcePermission(
            source_id=outlet.id,
            version=1,
            may_collect=True,
            may_store_full_text=False,
            may_republish_numbers=False,
            approved_at=datetime.now(UTC),
        )
    )
    session.flush()
    assert "No undecided domains" in admin.get("/admin/domains").text


# --- S-08: a news outlet needs an owner before it is approved -----------------------------------


def outlet(session: Session, owner: str | None = None) -> Source:
    source = make_source(session, "outlet-x", may_collect=True, approved_at=None)
    source.kind = "news_outlet"
    source.owner = owner
    session.flush()
    return source


def test_a_news_outlet_cannot_be_approved_without_an_owner(
    admin: TestClient, session: Session
) -> None:
    source = outlet(session)
    response = post(admin, f"/admin/sources/{source.id}/permissions/1/approve", terms_reviewed="on")
    assert response.status_code == 400 and "set the owner of this news outlet" in response.text
    permission = session.scalars(select(SourcePermission)).one()
    assert permission.approved_at is None
    assert "source_permission.approve" not in actions(session)


def test_a_news_outlet_with_an_owner_can_be_approved(admin: TestClient, session: Session) -> None:
    source = outlet(session)
    assert (
        post(admin, f"/admin/sources/{source.id}/owner", owner="Example Media Ltd").status_code
        == 303
    )
    session.refresh(source)
    assert source.owner == "Example Media Ltd" and "source.set_owner" in actions(session)
    response = post(admin, f"/admin/sources/{source.id}/permissions/1/approve", terms_reviewed="on")
    assert response.status_code == 303
    assert session.scalars(select(SourcePermission)).one().approved_at is not None


def test_publishing_a_new_collecting_version_for_an_outlet_without_an_owner_is_refused(
    admin: TestClient, session: Session
) -> None:
    source = outlet(session)
    response = post(
        admin,
        f"/admin/sources/{source.id}/permissions",
        may_collect="on",
        rights_basis="News: link and short quotation only",
        terms_reviewed="on",
    )
    assert response.status_code == 400 and "set the owner" in response.text
    # Withdrawing permission (no collection) never needs an owner.
    response = post(
        admin,
        f"/admin/sources/{source.id}/permissions",
        rights_basis="Withdrawn",
    )
    assert response.status_code == 303


def test_other_source_kinds_do_not_need_an_owner(admin: TestClient, session: Session) -> None:
    source = make_source(session, "stats", may_collect=True, approved_at=None)
    assert source.owner is None
    response = post(admin, f"/admin/sources/{source.id}/permissions/1/approve", terms_reviewed="on")
    assert response.status_code == 303


def test_the_owner_of_an_approved_outlet_cannot_be_blanked(
    admin: TestClient, session: Session
) -> None:
    source = outlet(session, "Example Media Ltd")
    post(admin, f"/admin/sources/{source.id}/permissions/1/approve", terms_reviewed="on")
    response = post(admin, f"/admin/sources/{source.id}/owner", owner="  ")
    assert response.status_code == 400 and "owner must stay on record" in response.text
    session.refresh(source)
    assert source.owner == "Example Media Ltd"
    same = post(admin, f"/admin/sources/{source.id}/owner", owner="Example Media Ltd")
    assert same.status_code == 400 and "already set" in same.text


def test_the_source_page_shows_the_owner_form(admin: TestClient, session: Session) -> None:
    source = outlet(session)
    page = admin.get(f"/admin/sources/{source.id}").text
    assert "cannot be approved until its owner is set" in page
    assert f"/admin/sources/{source.id}/owner" in page


# --- channel posts ------------------------------------------------------------------------------


def test_channel_posts_need_the_public_address(admin: TestClient) -> None:
    response = admin.get("/admin/channel-posts")
    assert response.status_code == 200 and "public address is not set" in response.text


def test_channel_posts_are_drafts_for_recent_material_changes(
    admin: TestClient, session: Session
) -> None:
    operator = make_operator(session, "x@example.org")
    settings_store.apply_changes(
        session, operator.operator, {"public_base_url": "https://africasignal.example"}
    )
    place = add_place(session, "NG", "Nigeria", "country")
    now = datetime.now(UTC)
    situation = add_situation(session, "pms_litre", place)
    good = add_version(
        session,
        situation,
        severity="high",
        published_at=now - timedelta(days=1),
        headline="Petrol rose 12% in Nigeria",
    )
    good.facts = [
        {
            "label": "Current price",
            "value": 1005.47,
            "unit": "NGN/litre",
            "period": "September 2026",
        },
        {"label": "Change", "value": 12, "unit": "%", "period": "September 2026"},
    ]
    broken = add_situation(session, "dpk_litre", place, title="Broken")
    add_version(
        session,
        broken,
        severity="high",
        published_at=now - timedelta(days=2),
        headline="Kerosene rose 99% in Nigeria",  # no fact states 99
    )
    old = add_situation(session, "ago_litre", place, title="Old one")
    add_version(session, old, severity="high", published_at=now - timedelta(days=30))
    page = admin.get("/admin/channel-posts").text
    assert "Drafts only" in page and "Petrol rose 12% in Nigeria" in page
    assert "?ref=wa" in page and "?ref=x" in page
    assert "1,005.47" in page  # the key number, as the facts state it
    assert "numbers not in the facts: 99" in page  # one bad post does not hide the good one
    assert "ago_litre" not in page
    assert "ago_litre" in admin.get("/admin/channel-posts?days=60").text
    assert not [a for a in actions(session) if "post" in a]  # nothing is recorded or sent


def test_channel_posts_are_paged_so_a_first_import_does_not_make_a_huge_page(
    admin: TestClient, session: Session
) -> None:
    operator = make_operator(session, "x@example.org")
    settings_store.apply_changes(
        session, operator.operator, {"public_base_url": "https://africasignal.example"}
    )
    place = add_place(session, "NG", "Nigeria", "country")
    now = datetime.now(UTC)
    for n in range(45):
        situation = add_situation(session, f"item_{n:02d}", place)
        version = add_version(
            session,
            situation,
            severity="high",
            published_at=now - timedelta(minutes=n),  # item_00 is the newest
            headline=f"Petrol rose {n + 1}% in Nigeria",
        )
        version.facts = [{"label": "Change", "value": n + 1, "unit": "%", "period": "June 2026"}]
    session.flush()

    first = admin.get("/admin/channel-posts").text
    assert first.count("<section>") == 20
    assert "item_00" in first and "item_19" in first and "item_20" not in first
    assert "Showing 1 to 20 of 45" in first
    assert 'href="/admin/channel-posts?days=7&amp;page=2">Next' in first
    last = admin.get("/admin/channel-posts?page=3").text
    assert last.count("<section>") == 5 and "item_44" in last
    assert "Next" not in last and "Previous" in last
    # a bad or out-of-range page number gives a real page, not an error
    assert "item_00" in admin.get("/admin/channel-posts?page=abc").text
    assert "item_44" in admin.get("/admin/channel-posts?page=99").text
    assert "item_00" in admin.get("/admin/channel-posts?page=-4").text


def test_assessments_page_is_paged_for_the_hold_queue_and_the_version_list(
    admin: TestClient, session: Session
) -> None:
    place = add_place(session, "NG", "Nigeria", "country")
    for n in range(60):
        situation = add_situation(session, f"item_{n:02d}", place)
        held = n < 45
        version = add_version(
            session,
            situation,
            status="draft" if held else "published",
            severity="high",
            published_at=None if held else datetime.now(UTC),
        )
        if held:
            version.hold_until = datetime.now(UTC) + timedelta(minutes=n + 1)
    session.flush()

    page = admin.get("/admin/assessments").text
    assert page.count("/withhold") == 20  # the hold queue shows 20, soonest release first
    assert (
        "item_00" in page
        and "item_19" in page
        and "item_20" not in page.split("Newest versions")[0]
    )
    assert "Showing 1 to 20 of 45" in page
    assert "Showing 1 to 50 of 60" in page  # the version list: 50 a page
    assert page.count('<a href="/s/') == 50
    second = admin.get("/admin/assessments?page=2&held_page=3").text
    assert second.count('<a href="/s/') == 10
    assert second.count("/withhold") == 5  # 45 - 40
    assert "Showing 41 to 45 of 45" in second
    # the status filter still works and keeps its own count
    published = admin.get("/admin/assessments?status=published").text
    assert "Showing 1 to 15 of 15" not in published  # one page needs no pager
    assert published.count('<a href="/s/') == 15


# --- backup alerts ------------------------------------------------------------------------------


def test_the_alerts_page_lists_open_and_resolved_alerts_and_the_drill(
    admin: TestClient, session: Session
) -> None:
    assert "No alert has been raised" in admin.get("/admin/alerts").text
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    stale = (now - timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    backup_alerts._write(session, backup_alerts.BACKUP_STATUS_KEY, {"last_success_at": stale})
    backup_alerts._write(
        session,
        backup_alerts.DRILL_STATUS_KEY,
        {"ok": False, "last_run_at": "2026-10-06T03:00:00Z", "detail": "dump would not restore"},
    )
    session.flush()
    backup_alerts.check(session, now)
    page = admin.get("/admin/alerts").text
    assert "backup_stale" in page and "OPEN" in page and "restore_drill_failed" in page
    assert "60.0 hours ago" in page
    assert "dump would not restore" in page and "failed" in page
    assert "alert.opened" in page  # the history from the audit log

    backup_alerts._write(
        session,
        backup_alerts.BACKUP_STATUS_KEY,
        {"last_success_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
    )
    session.flush()
    backup_alerts.check(session, now + timedelta(minutes=5))
    page = admin.get("/admin/alerts").text
    assert "alert.resolved" in page and "resolved" in page
    # The page only reads: it wrote nothing of its own.
    assert not [a for a in actions(session) if a.startswith("alert.") is False and "alert" in a]
