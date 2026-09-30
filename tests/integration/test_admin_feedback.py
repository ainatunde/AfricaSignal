"""Console pages for feedback and metrics (AS-034): the inbox, triage with an audit trail, and the
D1 numbers page."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import metrics, operators
from africasignal.models import AuditLog, Feedback, Operator, Situation
from africasignal.web.app import create_app
from africasignal.web.deps import get_db
from tests.integration.email_support import add_place, add_situation, add_version

PASSWORD = "correct horse battery staple"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    operators._last_step.clear()
    operators.throttle._failures.clear()


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        yield c


def make_operator(session: Session, email: str = "ops@example.org", role: str = "admin") -> Account:
    operator, secret = operators.create_operator(session, email, PASSWORD, role)
    return Account(operator, secret)


class Account:
    def __init__(self, operator: Operator, secret: str) -> None:
        self.operator = operator
        self.secret = secret


def signed_in(client: TestClient, account: Account) -> TestClient:
    form = {
        "email": account.operator.email,
        "password": PASSWORD,
        "code": pyotp.TOTP(account.secret).now(),
    }
    assert client.post("/admin/login", data=form, headers=ORIGIN).status_code == 303
    return client


def audit_actions(session: Session) -> list[str]:
    return list(session.scalars(select(AuditLog.action).order_by(AuditLog.id)))


@pytest.fixture
def situation(session: Session) -> Situation:
    situation = add_situation(session, "price-pms", add_place(session, "NG-LA", "Lagos", "state"))
    add_version(session, situation)
    return situation


def add_feedback(
    session: Session, situation: Situation, kind: str = "error_report", **kw: object
) -> Feedback:
    assert situation.current_version_id is not None
    row = Feedback(
        assessment_version_id=situation.current_version_id,
        kind=kind,
        anon_id="visitor-a-aaaaaaaaaaaaaaaa",
        **kw,  # type: ignore[arg-type]
    )
    session.add(row)
    session.flush()
    return row


def test_the_pages_need_a_sign_in(client: TestClient) -> None:
    for path in ("/admin/feedback", "/admin/feedback/1", "/admin/metrics"):
        assert client.get(path).headers["location"] == "/admin/login", path
    for path in ("/admin/feedback/1", "/admin/metrics/whatsapp", "/admin/metrics/infra"):
        assert client.post(path, headers=ORIGIN).headers["location"] == "/admin/login", path


def test_the_inbox_lists_open_error_reports_first(
    client: TestClient, session: Session, situation: Situation
) -> None:
    add_feedback(
        session, situation, text="The price is for diesel.", contact_email="ada@example.com"
    )
    add_feedback(session, situation, text="Old one", status="closed")
    add_feedback(session, situation, kind="useful_yes")
    signed_in(client, make_operator(session))
    page = client.get("/admin/feedback").text
    assert "The price is for diesel." in page and "Old one" not in page
    assert "<td>Useful: yes</td>" not in page  # votes are not reports
    everything = client.get("/admin/feedback?show=all&kind=all").text
    assert "Old one" in everything and "<td>Useful: yes</td>" in everything
    assert "Old one" in client.get("/admin/feedback?show=closed").text


def test_triage_changes_the_status_and_is_audited(
    client: TestClient, session: Session, situation: Situation
) -> None:
    row = add_feedback(
        session, situation, text="The price is for diesel.", contact_email="a@example.com"
    )
    signed_in(client, make_operator(session, role="editor"))  # editors triage too
    detail = client.get(f"/admin/feedback/{row.id}").text
    assert "The price is for diesel." in detail and "a@example.com" in detail

    response = client.post(
        f"/admin/feedback/{row.id}", data={"status": "investigating"}, headers=ORIGIN
    )
    assert response.status_code == 303
    session.refresh(row)
    assert row.status == "investigating" and row.resolved_at is None

    client.post(
        f"/admin/feedback/{row.id}",
        data={"status": "resolved_updated", "resolution_note": "Corrected on 2026-10-01."},
        headers=ORIGIN,
    )
    session.refresh(row)
    assert row.status == "resolved_updated" and row.resolution_note == "Corrected on 2026-10-01."
    assert row.resolved_at is not None
    entry = session.scalars(
        select(AuditLog).where(AuditLog.action == "feedback.update").order_by(AuditLog.id.desc())
    ).first()
    assert entry is not None and entry.target_id == row.id
    assert entry.before == {"status": "investigating", "resolution_note": None}
    assert entry.after == {
        "status": "resolved_updated",
        "resolution_note": "Corrected on 2026-10-01.",
    }
    assert audit_actions(session).count("feedback.update") == 2

    client.post(f"/admin/feedback/{row.id}", data={"status": "triaged"}, headers=ORIGIN)
    session.refresh(row)
    assert row.resolved_at is None  # reopened


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"status": "deleted"}, "Choose a status"),
        ({"status": "closed"}, "Add a short note"),
        ({"status": "closed", "resolution_note": "x" * 2001}, "under 2000"),
    ],
)
def test_bad_triage_is_refused(
    client: TestClient, session: Session, situation: Situation, data: dict[str, str], message: str
) -> None:
    row = add_feedback(session, situation, text="Wrong.")
    signed_in(client, make_operator(session))
    response = client.post(f"/admin/feedback/{row.id}", data=data, headers=ORIGIN)
    assert response.status_code == 400 and message in response.text
    session.refresh(row)
    assert row.status == "received"
    assert "feedback.update" not in audit_actions(session)
    assert client.get("/admin/feedback/99999").status_code == 404


def test_the_console_nav_lists_the_new_pages(client: TestClient, session: Session) -> None:
    signed_in(client, make_operator(session))
    page = client.get("/admin/feedback").text
    assert 'href="/admin/metrics"' in page and 'href="/admin/feedback"' in page


def test_the_metrics_page_shows_the_d1_numbers(
    client: TestClient, session: Session, situation: Situation
) -> None:
    now = datetime.now(UTC)
    for anon in ("visitor-a-aaaaaaaaaaaaaaaa", "visitor-b-bbbbbbbbbbbbbbbb"):
        metrics.record_event(session, "page_view", anon_id=anon, ref="wa", now=now)
    session.flush()
    signed_in(client, make_operator(session))
    page = client.get("/admin/metrics").text
    assert "WhatsApp channel followers" in page and "Visitors who return within 14 days" in page
    assert "Cost per weekly active visitor" in page and "Not enough data" in page
    assert metrics.iso_week_label(now) in page and "in progress" in page
    assert "Save follower count" in page


def test_an_admin_records_the_numbers_from_outside(client: TestClient, session: Session) -> None:
    week = metrics.iso_week_label(datetime.now(UTC))
    signed_in(client, make_operator(session))
    response = client.post(
        "/admin/metrics/whatsapp", data={"week": week, "followers": "512"}, headers=ORIGIN
    )
    assert response.status_code == 303
    assert metrics.whatsapp_followers(session) == {week: 512}
    client.post("/admin/metrics/infra", data={"monthly_usd": "26.00"}, headers=ORIGIN)
    assert metrics.infra_monthly_usd(session) == Decimal("26.00")
    page = client.get("/admin/metrics").text
    assert ">512<" in page and "$26.00" in page
    assert {"metrics.whatsapp_followers", "metrics.infra_cost"} <= set(audit_actions(session))


def test_bad_numbers_show_an_error(client: TestClient, session: Session) -> None:
    signed_in(client, make_operator(session))
    for path, data in (
        ("/admin/metrics/whatsapp", {"week": "next week", "followers": "5"}),
        ("/admin/metrics/whatsapp", {"week": "2026-W40", "followers": "many"}),
        ("/admin/metrics/infra", {"monthly_usd": "lots"}),
        ("/admin/metrics/infra", {"monthly_usd": "-4"}),
    ):
        response = client.post(path, data=data, headers=ORIGIN)
        assert response.status_code == 400 and 'role="alert"' in response.text, data
    assert metrics.whatsapp_followers(session) == {}


def test_editors_can_read_but_not_enter_numbers(client: TestClient, session: Session) -> None:
    signed_in(client, make_operator(session, role="editor"))
    page = client.get("/admin/metrics").text
    assert "Save follower count" not in page and "An admin enters" in page
    for path, data in (
        ("/admin/metrics/whatsapp", {"week": "2026-W40", "followers": "5"}),
        ("/admin/metrics/infra", {"monthly_usd": "5"}),
    ):
        assert client.post(path, data=data, headers=ORIGIN).status_code == 403
    assert metrics.whatsapp_followers(session) == {}
