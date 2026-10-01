"""Feedback and events on the public site (AS-034): "Was this useful?", error reports, the rate
limit, page views, share clicks and the digest's tracking pixel. No IP address is stored."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from africasignal import metrics
from africasignal.models import AppUser, Event, Feedback, Situation
from africasignal.publish import email_render
from africasignal.web.routes.feedback import MAX_FEEDBACK_PER_DAY
from tests.integration.email_support import add_place, add_situation, add_user, add_version
from tests.integration.web.web_support import ORIGIN

SLUG = "price-pms"
ANON = "a-returning-visitor-1234"


@pytest.fixture
def situation(session: Session) -> Situation:
    country = add_place(session, "NG", "Nigeria", "country")
    state = add_place(session, "NG-LA", "Lagos", "state", country)
    situation = add_situation(session, SLUG, state)
    add_version(session, situation)
    return situation


@pytest.fixture
def visitor(client: TestClient) -> TestClient:
    client.cookies.set(metrics.ANON_COOKIE, ANON)
    return client


def feedback_rows(session: Session) -> list[Feedback]:
    return list(session.scalars(select(Feedback).order_by(Feedback.id)))


# --- "Was this useful?" -------------------------------------------------------------------------


def test_useful_answers_are_saved_against_the_current_version(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    response = visitor.post(
        f"/s/{SLUG}/useful", data={"answer": "yes"}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/s/{SLUG}/thanks?k=useful"
    (row,) = feedback_rows(session)
    assert row.kind == "useful_yes" and row.anon_id == ANON and row.user_id is None
    assert row.assessment_version_id == situation.current_version_id
    assert row.status == "received" and row.text is None
    assert "Thanks for telling us" in visitor.get(f"/s/{SLUG}/thanks?k=useful").text
    events = session.scalars(select(Event).where(Event.name == "feedback")).all()
    assert [e.props for e in events] == [{"kind": "useful_yes"}]


def test_changing_your_mind_is_not_a_second_vote(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    visitor.post(f"/s/{SLUG}/useful", data={"answer": "yes"}, headers=ORIGIN)
    visitor.post(f"/s/{SLUG}/useful", data={"answer": "no"}, headers=ORIGIN)
    (row,) = feedback_rows(session)
    assert row.kind == "useful_no"


def test_an_unknown_answer_or_situation_is_ignored(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    visitor.post(f"/s/{SLUG}/useful", data={"answer": "maybe"}, headers=ORIGIN)
    assert visitor.post("/s/nope/useful", data={"answer": "yes"}, headers=ORIGIN).status_code == 404
    assert feedback_rows(session) == []


def test_a_first_time_visitor_gets_a_cookie_when_they_answer(
    client: TestClient, session: Session, situation: Situation
) -> None:
    response = client.post(
        f"/s/{SLUG}/useful", data={"answer": "no"}, headers=ORIGIN, follow_redirects=False
    )
    cookie = response.headers["set-cookie"]
    assert "anon_id=" in cookie and "HttpOnly" in cookie
    (row,) = feedback_rows(session)
    assert row.anon_id and row.anon_id in cookie


# --- error reports ------------------------------------------------------------------------------


def test_an_error_report(visitor: TestClient, session: Session, situation: Situation) -> None:
    form = visitor.get(f"/s/{SLUG}/report")
    assert form.status_code == 200 and 'name="text"' in form.text
    response = visitor.post(
        f"/s/{SLUG}/report",
        data={"text": "  The price is for diesel, not petrol.  ", "contact": "Ada@Example.com"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/s/{SLUG}/thanks?k=report"
    (row,) = feedback_rows(session)
    assert row.kind == "error_report" and row.text == "The price is for diesel, not petrol."
    assert row.contact_email == "ada@example.com" and row.anon_id == ANON


def test_the_contact_email_is_optional_and_only_stored_when_typed(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    visitor.post(f"/s/{SLUG}/report", data={"text": "Wrong month."}, headers=ORIGIN)
    (row,) = feedback_rows(session)
    assert row.contact_email is None


def test_a_signed_in_reader_is_linked_to_their_report(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    from tests.integration.web.test_web_accounts import sign_in

    sign_in(visitor, session)
    user = session.scalars(select(AppUser)).one()
    visitor.post(f"/s/{SLUG}/report", data={"text": "Wrong month."}, headers=ORIGIN)
    (row,) = feedback_rows(session)
    assert row.user_id == user.id


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"text": "   "}, "tell us what looks wrong"),
        ({"text": "x" * 2001}, "under 2000 characters"),
        ({"text": "Wrong.", "contact": "not-an-email"}, "does not look like an email"),
    ],
)
def test_a_bad_report_is_refused_and_keeps_what_was_typed(
    visitor: TestClient, session: Session, situation: Situation, data: dict[str, str], message: str
) -> None:
    response = visitor.post(f"/s/{SLUG}/report", data=data, headers=ORIGIN)
    assert response.status_code == 400 and message in response.text
    assert feedback_rows(session) == []
    if data.get("contact"):
        assert 'value="not-an-email"' in response.text


def test_a_report_of_exactly_2000_characters_is_accepted(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    visitor.post(f"/s/{SLUG}/report", data={"text": "x" * 2000}, headers=ORIGIN)
    assert len(feedback_rows(session)[0].text or "") == 2000


def test_a_filled_honeypot_is_dropped_quietly(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    response = visitor.post(
        f"/s/{SLUG}/report",
        data={"text": "buy pills", "website": "http://spam.example"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303 and feedback_rows(session) == []


def test_ten_submissions_a_day_per_visitor(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    for i in range(MAX_FEEDBACK_PER_DAY):
        response = visitor.post(
            f"/s/{SLUG}/report",
            data={"text": f"Report {i}"},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert response.status_code == 303
    over = visitor.post(f"/s/{SLUG}/report", data={"text": "One more"}, headers=ORIGIN)
    assert over.status_code == 429 and "try again tomorrow" in over.text
    assert len(feedback_rows(session)) == MAX_FEEDBACK_PER_DAY
    # A new vote on a fresh version is over the limit too, and another visitor is not affected.
    assert (
        visitor.post(f"/s/{SLUG}/useful", data={"answer": "yes"}, headers=ORIGIN).status_code == 429
    )
    visitor.cookies.set(metrics.ANON_COOKIE, "another-visitor-5678-xyz")
    assert (
        visitor.post(
            f"/s/{SLUG}/report", data={"text": "Mine"}, headers=ORIGIN, follow_redirects=False
        ).status_code
        == 303
    )


def test_the_limit_counts_the_last_24_hours(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    assert situation.current_version_id is not None
    for _ in range(MAX_FEEDBACK_PER_DAY):
        row = Feedback(
            assessment_version_id=situation.current_version_id,
            anon_id=ANON,
            kind="error_report",
            text="old",
        )
        session.add(row)
        session.flush()
        row.created_at = row.created_at - timedelta(hours=25)
    session.flush()
    response = visitor.post(
        f"/s/{SLUG}/report", data={"text": "New"}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303


# --- page views ---------------------------------------------------------------------------------


def events(session: Session, name: str | None = None) -> list[Event]:
    query = select(Event).order_by(Event.id)
    if name:
        query = query.where(Event.name == name)
    return list(session.scalars(query))


def test_a_situation_page_records_a_page_view_and_a_situation_view(
    client: TestClient, session: Session, situation: Situation
) -> None:
    response = client.get(f"/s/{SLUG}?ref=wa")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "anon_id=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert response.headers["cache-control"] == "private, no-cache"  # it set a cookie

    views = events(session)
    assert [e.name for e in views] == ["page_view", "situation_view"]
    assert all(e.ref == "wa" and e.situation_id == situation.id for e in views)
    assert views[0].props == {"page": "situation"}
    assert views[0].anon_id and views[0].anon_id in cookie and views[0].user_id is None


def test_a_returning_visitor_keeps_their_cookie(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    response = visitor.get(f"/s/{SLUG}")
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "public, max-age=300"
    assert {e.anon_id for e in events(session)} == {ANON}


def test_only_known_referrers_are_kept(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    for ref in ("x", "email", "share", "campaign-42", "WA"):
        visitor.get(f"/s/{SLUG}", params={"ref": ref})
    assert [e.ref for e in events(session, "page_view")] == ["x", "email", "share", None, None]


def test_other_pages_count_as_page_views_with_no_situation(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    for path in (
        "/",
        "/explore",
        "/coverage",
        "/about/method",
        f"/s/{SLUG}/history",
        "/places/NG-LA",
    ):
        visitor.get(path)
    views = events(session, "page_view")
    assert [e.props["page"] for e in views] == [
        "home",
        "explore",
        "coverage",
        "method",
        "history",
        "place",
    ]
    assert events(session, "situation_view") == []


def test_crawlers_link_previews_and_missing_pages_are_not_counted(
    client: TestClient, session: Session, situation: Situation
) -> None:
    for agent in (
        "WhatsApp/2.23.20.0",
        "facebookexternalhit/1.1",
        "Googlebot/2.1 (+http://www.google.com/bot.html)",
        "AfricaSignalBot/1.0",
    ):
        response = client.get(f"/s/{SLUG}", headers={"User-Agent": agent})
        assert response.status_code == 200 and "set-cookie" not in response.headers
    assert client.get("/s/nope").status_code == 404
    assert client.get("/places/ZZ").status_code == 404
    assert events(session) == []


def test_pages_with_secrets_in_the_address_and_the_api_are_not_recorded(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    visitor.get("/signin/verify", params={"token": "t" * 43})
    visitor.get("/unsubscribe", params={"t": "1.abc"})
    visitor.get("/signin")
    visitor.get("/v1/coverage")
    visitor.get("/healthz")
    visitor.get("/static/site.css")
    assert events(session) == []


def test_no_address_or_user_agent_is_stored(
    client: TestClient, session: Session, situation: Situation
) -> None:
    client.get(f"/s/{SLUG}", headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13)"})
    columns = {c.key for c in inspect(Event).column_attrs}
    assert columns == {
        "id",
        "created_at",
        "ts",
        "anon_id",
        "user_id",
        "name",
        "situation_id",
        "ref",
        "props",
    }
    stored = " ".join(str(getattr(e, c)) for e in events(session) for c in columns)
    assert "Mozilla" not in stored and "testclient" not in stored and "testserver" not in stored


def test_a_failure_to_record_never_breaks_the_page(
    client: TestClient, situation: Situation
) -> None:
    def broken() -> None:
        raise RuntimeError("database down")

    client.app.state.event_session = broken  # type: ignore[attr-defined]
    assert client.get(f"/s/{SLUG}").status_code == 200


def test_a_signed_in_readers_views_carry_their_user(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    from tests.integration.web.test_web_accounts import sign_in

    sign_in(visitor, session)
    user = session.scalars(select(AppUser)).one()
    visitor.get(f"/s/{SLUG}")
    assert {e.user_id for e in events(session, "situation_view")} == {user.id}


# --- share clicks and the digest pixel ----------------------------------------------------------


def test_the_share_beacon_records_a_share_click(
    visitor: TestClient, session: Session, situation: Situation
) -> None:
    assert visitor.post(f"/s/{SLUG}/share", headers=ORIGIN).status_code == 204
    (click,) = events(session, "share_click")
    assert click.anon_id == ANON and click.situation_id == situation.id
    assert (
        visitor.post("/s/nope/share", headers=ORIGIN).status_code == 204
    )  # the page does not wait
    assert len(events(session, "share_click")) == 1


def test_the_situation_page_sends_the_beacon_address(
    client: TestClient, situation: Situation
) -> None:
    assert f'data-beacon="/s/{SLUG}/share"' in client.get(f"/s/{SLUG}").text


def digest_pixel(user: AppUser, week: str = "2026-W41") -> str:
    url = email_render.digest_open_url(user.id, week, "http://testserver")
    return url.removeprefix("http://testserver")


def test_the_digest_pixel_counts_one_open_per_week(client: TestClient, session: Session) -> None:
    user = add_user(session, "ada@example.com", digest=True)
    for _ in range(3):
        response = client.get(digest_pixel(user))
        assert response.status_code == 200 and response.headers["content-type"] == "image/gif"
        assert response.content.startswith(b"GIF89a")
        assert response.headers["cache-control"] == "no-store"
    client.get(digest_pixel(user, "2026-W42"))
    opens = events(session, "digest_open")
    assert [(e.user_id, e.ref, e.props["week"], e.anon_id) for e in opens] == [
        (user.id, "email", "2026-W41", None),
        (user.id, "email", "2026-W42", None),
    ]


def test_the_pixel_tracks_only_readers_who_opted_in(client: TestClient, session: Session) -> None:
    out = add_user(session, "out@example.com", digest=False)
    assert client.get(digest_pixel(out)).status_code == 200
    assert client.get("/e/o/7:2026-W41.forged.gif").status_code == 200
    assert client.get("/e/o/garbage.gif").status_code == 200
    assert events(session, "digest_open") == []


def test_the_digest_email_carries_the_pixel(session: Session) -> None:
    payload = {"week": "2026-W41", "followed": [], "top": []}
    message = email_render.render_digest("a@example.com", 7, payload, "k", base="https://x.example")
    assert message.html is not None and 'src="https://x.example/e/o/7:2026-W41.' in message.html
    assert "/e/o/" not in message.text  # the plain-text part has none
