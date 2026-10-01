# ruff: noqa: F811  (fixtures imported from test_admin_ops are redefined as test arguments)
"""Security re-check of the operator console pages (2026-10-01): one regression test for each
finding. See reviews/africasignal-security-review.md, section "Console pages"."""

from __future__ import annotations

import io
import signal
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.catalog import load_items
from africasignal.models import (
    AssessmentVersion,
    AuditLog,
    DiscoveredDomainDecision,
    Job,
    Situation,
    Source,
)
from africasignal.operations import jobs as job_ops
from africasignal.publish.situations import source_is_trusted
from africasignal.sources.nbs_workbook import NbsParseError, parse_workbook
from africasignal.sources.seed import SeedSource, seed_sources
from africasignal.storage import S3Store
from tests.integration.email_support import add_place, add_situation, add_version
from tests.integration.outlet_support import NOW, approved_source
from tests.integration.test_admin_console import ORIGIN, client  # noqa: F401
from tests.integration.test_admin_ops import (  # noqa: F401  (fixtures and helpers)
    PMS_OCT,
    actions,
    admin,
    discover,
    held_version,
    make_job,
    nbs,
    post,
    source,
    store,
    upload,
)
from tests.unit.sources.nbs_fixtures import read


@pytest.fixture
def quickly() -> Iterator[None]:
    """Fail instead of hanging: the workbook bomb used to run for hours."""

    def give_up(*_: object) -> None:
        raise TimeoutError("took too long")

    old = signal.signal(signal.SIGALRM, give_up)
    signal.alarm(20)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


# --- NBS upload: a hostile file ----------------------------------------------------------------


def bomb() -> bytes:
    """A few kilobytes that declare a sheet reaching cell XFD1048576."""
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws["A1"] = "x"
    ws.cell(row=1_048_576, column=16_384, value=1)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_a_sheet_that_declares_a_huge_extent_is_refused_unread(quickly: None) -> None:
    data = bomb()
    assert len(data) < 10_000
    with pytest.raises(NbsParseError, match="far larger than a price table"):
        parse_workbook(data, load_items().publication("pms"))


def test_a_real_workbook_is_still_read() -> None:
    parsed = parse_workbook(read(PMS_OCT), load_items().publication("pms"))
    assert parsed.tables


def test_an_upload_without_a_declared_size_is_refused(
    admin: TestClient, session: Session, nbs: Source
) -> None:
    """A chunked body has no Content-Length, and the form parser would spool it without limit."""
    boundary = "xBOUNDARYx"
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="source_id"\r\n\r\n{nbs.id}\r\n'
    body = (
        (
            head
            + f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.xlsx"\r\n'
            + "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + b"PK\x03\x04"
        + f"\r\n--{boundary}--\r\n".encode()
    )

    def chunks() -> Iterator[bytes]:
        yield body[:50]
        yield body[50:]

    response = admin.post(
        "/admin/nbs-upload",
        content=chunks(),
        headers={**ORIGIN, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert response.status_code == 411
    assert session.scalars(select(Job).where(Job.kind == "import_nbs_file")).first() is None


def test_an_upload_declaring_more_than_the_cap_is_refused(admin: TestClient, nbs: Source) -> None:
    response = admin.post(
        "/admin/nbs-upload",
        content=b"x",
        headers={
            **ORIGIN,
            "Content-Type": "multipart/form-data; boundary=b",
            "Content-Length": str(50_000_000),
        },
    )
    assert response.status_code in (413, 400)


def test_text_fields_with_control_characters_are_cleaned_not_a_server_error(
    admin: TestClient, session: Session, nbs: Source
) -> None:
    response = upload(
        admin,
        read(PMS_OCT),
        source_id=str(nbs.id),
        title="Premium Motor Spirit\x00 (Petrol) Price Watch (October 2024)",
    )
    assert response.status_code == 303
    job = session.scalars(select(Job).where(Job.kind == "import_nbs_file")).one()
    assert "\x00" not in job.payload["title"]


# --- discovered domains: nothing hostile in the new source -------------------------------------


@pytest.mark.parametrize(
    "feed_url",
    [
        "https://evilnews.example/feed",  # a look-alike, not a subdomain
        "https://news.example.evil.example/feed",
        "https://news.example@evil.example/feed",
        "https://evil.example/feed#news.example",
        "https://news.example/feed\nhttps://evil.example/x",
        "https://news.example/" + "a" * 2000,
    ],
    ids=["lookalike", "suffix", "userinfo", "fragment", "newline", "too-long"],
)
def test_the_feed_address_must_really_be_on_the_domain(
    admin: TestClient, session: Session, feed_url: str
) -> None:
    discover(session, "https://news.example/a")
    response = post(
        admin,
        "/admin/domains/add",
        domain="news.example",
        name="News Example",
        owner="Example Media Ltd",
        feed_url=feed_url,
    )
    assert response.status_code == 400
    assert session.scalars(select(Source).where(Source.slug.like("news-example%"))).first() is None
    assert session.scalars(select(DiscoveredDomainDecision)).first() is None


def test_names_and_notes_with_control_characters_are_cleaned(
    admin: TestClient, session: Session
) -> None:
    discover(session, "https://news.example/a", "https://spam.example/a")
    assert (
        post(
            admin, "/admin/domains/reject", domain="spam.example", note="farm\x00\u202e of ads"
        ).status_code
        == 303
    )
    note = session.scalars(select(DiscoveredDomainDecision.note)).one()
    assert note == "farm of ads"
    response = post(
        admin,
        "/admin/domains/add",
        domain="news.example",
        name="News\x00 Example\u200b",
        owner="Example\u202e Media Ltd",
        feed_url="https://news.example/feed/",
    )
    assert response.status_code == 303
    created = session.scalars(select(Source).where(Source.slug == "news-example-rss")).one()
    assert (created.name, created.owner) == ("News Example", "Example Media Ltd")


# --- the owner rule cannot be bypassed ---------------------------------------------------------


def test_a_seed_run_does_not_blank_an_owner_an_operator_recorded(session: Session) -> None:
    entry = SeedSource(
        slug="punch-rss",
        name="Punch",
        kind="news_outlet",
        adapter="rss",
        schedule_minutes=30,
        feed_url="https://rss.punchng.com/feed",
    )
    assert entry.owner is None  # the shipped sources.yaml names no owner for news outlets
    seed_sources(session, [entry])
    created = session.scalars(select(Source).where(Source.slug == "punch-rss")).one()
    created.owner = "Punch Nigeria Ltd"  # what the console's owner form stores
    session.flush()
    seed_sources(session, [entry])  # the compose "migrate" service runs this on every deploy
    session.refresh(created)
    assert created.owner == "Punch Nigeria Ltd"


def test_a_seed_with_an_owner_still_sets_it(session: Session) -> None:
    entry = SeedSource(
        slug="nbs-x",
        name="NBS",
        kind="official_statistics",
        adapter="nbs",
        owner="National Bureau of Statistics",
        schedule_minutes=60,
    )
    seed_sources(session, [entry])
    row = session.scalars(select(Source).where(Source.slug == "nbs-x")).one()
    row.owner = None
    session.flush()
    seed_sources(session, [entry])
    session.refresh(row)
    assert row.owner == "National Bureau of Statistics"


def test_an_approved_news_outlet_without_an_owner_is_not_trusted(session: Session) -> None:
    """Whatever left the owner blank (a seed run, an approval from before the rule, a database
    edit), the outlet cannot corroborate anything."""
    blank = approved_source(session, "blank-outlet", owner=None)
    named = approved_source(session, "named-outlet", owner="Named Media Ltd")
    spaces = approved_source(session, "space-outlet", owner="   ")
    official = approved_source(session, "stats", kind="official_statistics", owner=None)
    assert not source_is_trusted(session, blank, NOW)
    assert not source_is_trusted(session, spaces, NOW)
    assert source_is_trusted(session, named, NOW)
    assert source_is_trusted(session, official, NOW)


# --- withdrawal text readers see ---------------------------------------------------------------


def current_situation(session: Session) -> Situation:
    place = add_place(session, "NG", "Nigeria", "country")
    situation = add_situation(session, "pms", place)
    add_version(session, situation)
    return situation


@pytest.mark.parametrize(
    "reason",
    [
        "see https://evil.example/claim for the real story",
        "see www.evil.example for the real story",
        "\uff48\uff54\uff54\uff50\uff53\uff1a//evil.example is the real story",  # full-width characters
        "contact me at someone@example.org about it",
        "the <b>NBS</b> file was withdrawn",
        "the [real story](x) is elsewhere",
        "run `this` instead of that one",
    ],
    ids=["https", "www", "fullwidth", "email", "markup", "markdown-link", "backticks"],
)
def test_a_withdrawal_reason_is_plain_text_without_links(
    admin: TestClient, session: Session, reason: str
) -> None:
    situation = current_situation(session)
    response = post(admin, f"/admin/assessments/situations/{situation.id}/withdraw", reason=reason)
    assert response.status_code == 400 and "plain text" in response.text
    session.refresh(situation)
    assert session.get(AssessmentVersion, situation.current_version_id).status == "published"  # type: ignore[union-attr]
    assert "assessment.withdraw" not in actions(session)


def test_invisible_and_direction_characters_never_reach_readers(
    admin: TestClient, session: Session
) -> None:
    situation = current_situation(session)
    response = post(
        admin,
        f"/admin/assessments/situations/{situation.id}/withdraw",
        reason="the NBS file was \u202ewithdrawn\u200b by\x00 the publisher",
    )
    assert response.status_code == 303
    session.refresh(situation)
    new = session.get(AssessmentVersion, situation.current_version_id)
    assert new is not None
    assert new.headline == "Withdrawn: the NBS file was withdrawn by the publisher"
    assert new.change_summary == "The NBS file was withdrawn by the publisher"
    page = admin.get("/admin/assessments").text
    assert "\u202e" not in page and "\u200b" not in page


# --- publishing early ---------------------------------------------------------------------------


def test_publishing_early_is_refused_when_a_newer_version_exists(
    admin: TestClient, session: Session, store: S3Store, source: Source
) -> None:
    """Otherwise the older held figures would replace whatever the newer version says."""
    version = held_version(session, store, source)
    situation = session.get(Situation, version.situation_id)
    assert situation is not None
    add_version(
        session,
        situation,
        version=version.version + 1,
        status="withheld",
        current=False,
        published_at=None,
    )
    response = post(
        admin,
        f"/admin/assessments/versions/{version.id}/release",
        reason="checked against the NBS bulletin",
    )
    assert response.status_code == 400 and "a newer version" in response.text
    session.refresh(version)
    assert version.status == "draft" and version.hold_until is not None
    assert situation.current_version_id is None
    assert "assessment.release_early" not in actions(session)


# --- the Jobs page -----------------------------------------------------------------------------

DB_ERROR = (
    "IntegrityError: (psycopg.errors.UniqueViolation) duplicate key\n"
    "[SQL: INSERT INTO outbox (recipient) VALUES (%(recipient)s)]\n"
    "[parameters: {'recipient': 'reader@example.org'}]\n"
    "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
)


def test_a_database_error_is_shown_without_its_statement_or_values(
    admin: TestClient, session: Session
) -> None:
    job = make_job(session, "dead", error=DB_ERROR)
    assert (
        job_ops.short_error(job) == "IntegrityError: (psycopg.errors.UniqueViolation) duplicate key"
    )
    page = admin.get("/admin/jobs").text
    assert "UniqueViolation" in page
    assert "reader@example.org" not in page and "INSERT INTO" not in page


def test_retrying_a_job_does_not_copy_its_error_text_into_the_audit_log(
    admin: TestClient, session: Session
) -> None:
    """Every operator can read the audit log, including editors."""
    job = make_job(session, "dead", error=DB_ERROR)
    post(admin, f"/admin/jobs/{job.id}/retry")
    row = session.scalars(select(AuditLog).where(AuditLog.action == "job.retry")).one()
    assert row.before == {"status": "dead", "attempts": 5, "error_type": "IntegrityError"}
    assert "reader@example.org" not in str(row.before) + str(row.after)


# Keep a reference so linters do not drop imports used only by fixtures.
_ = (UTC, datetime, timedelta)
