"""Channel posts: marking a draft as posted by hand (AS-033). The app sends nothing; the record
says that a person did."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import settings_store
from africasignal.models import AssessmentVersion, AuditLog, ChannelPost, Outbox, Situation
from africasignal.publish.versions import set_publication_suspended
from tests.integration.email_support import add_place, add_situation, add_version
from tests.integration.test_admin_console import (
    ORIGIN,
    client,  # noqa: F401  (fixture)
    make_operator,
    signed_in,
)

FACTS = [
    {"label": "Current price", "value": 1005.47, "unit": "NGN/litre", "period": "September 2026"},
    {"label": "Change", "value": 12, "unit": "%", "period": "September 2026"},
]


@pytest.fixture
def admin(client: TestClient, session: Session) -> TestClient:  # noqa: F811
    operator = make_operator(session)
    settings_store.apply_changes(
        session, operator.operator, {"public_base_url": "https://africasignal.example"}
    )
    return signed_in(client, operator)


@pytest.fixture
def version(session: Session) -> AssessmentVersion:
    place = add_place(session, "NG", "Nigeria", "country")
    situation = add_situation(session, "pms_litre", place)
    made = add_version(
        session,
        situation,
        severity="high",
        published_at=datetime.now(UTC) - timedelta(days=1),
        headline="Petrol rose 12% in Nigeria",
    )
    made.facts = FACTS
    session.flush()
    return made


def mark(admin: TestClient, version_id: int, channel: str = "wa", **form: str):  # type: ignore[no-untyped-def]
    return admin.post(
        f"/admin/channel-posts/versions/{version_id}/{channel}/posted", data=form, headers=ORIGIN
    )


def audit_actions(session: Session) -> list[str]:
    return list(session.scalars(select(AuditLog.action).order_by(AuditLog.id)))


def test_marking_a_draft_as_posted_records_it_and_sends_nothing(
    admin: TestClient, session: Session, version: AssessmentVersion
) -> None:
    assert "Mark as posted on WhatsApp" in admin.get("/admin/channel-posts").text
    response = mark(admin, version.id, "wa", post_url="https://wa.example/c/123", note="  sent  ")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/channel-posts?notice=post_marked"
    row = session.scalars(select(ChannelPost)).one()
    assert (row.channel, row.post_url, row.note) == ("wa", "https://wa.example/c/123", "sent")
    assert row.assessment_version_id == version.id and row.posted_at is not None
    assert audit_actions(session)[-1] == "channel_post.mark_posted"
    assert session.scalars(select(Outbox)).all() == []  # nothing was queued to send

    page = admin.get("/admin/channel-posts").text
    assert "Posted on WhatsApp" in page and "Mark as posted on X" in page
    assert "Mark as posted on WhatsApp" not in page
    assert "https://wa.example/c/123" in page  # also in the recently-marked table


def test_each_channel_is_marked_separately_and_only_once(
    admin: TestClient, session: Session, version: AssessmentVersion
) -> None:
    assert mark(admin, version.id, "wa").status_code == 303
    again = mark(admin, version.id, "wa")
    assert again.status_code == 400 and "already marked as posted on WhatsApp" in again.text
    assert mark(admin, version.id, "x").status_code == 303
    assert sorted(session.scalars(select(ChannelPost.channel))) == ["wa", "x"]


@pytest.mark.parametrize(
    ("channel", "form", "message"),
    [
        ("sms", {}, "choose WhatsApp or X"),
        ("wa", {"post_url": "http://insecure.example/p"}, "plain https address"),
        ("wa", {"post_url": "javascript:alert(1)"}, "plain https address"),
        ("wa", {"post_url": "https://user:pw@host.example/p"}, "plain https address"),
        ("wa", {"post_url": "https://h.example/" + "a" * 300}, "plain https address"),
        ("wa", {"note": "n" * 201}, "keep the note under"),
    ],
)
def test_bad_input_is_refused(
    admin: TestClient,
    session: Session,
    version: AssessmentVersion,
    channel: str,
    form: dict[str, str],
    message: str,
) -> None:
    response = mark(admin, version.id, channel, **form)
    assert response.status_code == 400 and message in response.text
    assert session.scalars(select(ChannelPost)).all() == []


def test_only_the_current_published_version_can_be_marked(
    admin: TestClient, session: Session, version: AssessmentVersion
) -> None:
    assert mark(admin, 999_999).status_code == 400
    version.status = "withheld"
    session.flush()
    response = mark(admin, version.id)
    assert response.status_code == 400 and "no longer the published one" in response.text
    version.status = "published"
    situation = session.get(Situation, version.situation_id)
    assert situation is not None
    situation.current_version_id = None
    session.flush()
    assert mark(admin, version.id).status_code == 400
    assert session.scalars(select(ChannelPost)).all() == []


def test_while_publication_is_suspended_there_are_no_drafts_and_nothing_can_be_marked(
    admin: TestClient, session: Session, version: AssessmentVersion
) -> None:
    set_publication_suspended(session, True, datetime.now(UTC))
    page = admin.get("/admin/channel-posts")
    assert page.status_code == 200
    assert "Publication is suspended" in page.text and "Petrol rose 12%" not in page.text
    response = mark(admin, version.id)
    assert response.status_code == 400 and "publication is suspended" in response.text
    assert session.scalars(select(ChannelPost)).all() == []


def test_a_mistaken_record_can_be_undone_and_the_audit_row_keeps_it(
    admin: TestClient, session: Session, version: AssessmentVersion
) -> None:
    mark(admin, version.id, "x", note="oops")
    record = session.scalars(select(ChannelPost)).one()
    response = admin.post(f"/admin/channel-posts/records/{record.id}/undo", headers=ORIGIN)
    assert response.headers["location"].endswith("notice=post_unmarked")
    assert session.scalars(select(ChannelPost)).all() == []
    entry = session.scalars(select(AuditLog).where(AuditLog.action == "channel_post.unmark")).one()
    assert entry.before is not None and entry.before["note"] == "oops"
    assert admin.post("/admin/channel-posts/records/999/undo", headers=ORIGIN).status_code == 400
    # it can be marked again
    assert mark(admin, version.id, "x").status_code == 303


def test_editors_and_other_origins_are_refused(
    client: TestClient,  # noqa: F811
    session: Session,
    version: AssessmentVersion,
) -> None:
    signed_in(client, make_operator(session, "editor@example.org", "editor"))
    assert mark(client, version.id).status_code == 403
    assert client.post("/admin/channel-posts/records/1/undo", headers=ORIGIN).status_code == 403
    admin_client = signed_in(client, make_operator(session, "boss@example.org"))
    response = admin_client.post(
        f"/admin/channel-posts/versions/{version.id}/wa/posted",
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert session.scalars(select(ChannelPost)).all() == []


def test_control_characters_are_cleaned_not_a_server_error(
    admin: TestClient, session: Session, version: AssessmentVersion
) -> None:
    response = mark(
        admin, version.id, "wa", note="sent\x00 it\u202e", post_url="https://h.example/p\x00"
    )
    assert response.status_code == 303
    assert "\x00" not in str(session.scalars(select(ChannelPost.note)).all())
