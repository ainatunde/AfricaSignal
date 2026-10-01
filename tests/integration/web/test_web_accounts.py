"""Accounts on the web (AS-031, web half): magic-link sign-in that survives mail scanners, follows,
in-site notifications, the account page, deletion, and the unsubscribe page (AS-032, web half)."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal.models import (
    AppUser,
    Event,
    Follow,
    Job,
    Notification,
    Outbox,
    Place,
    Preference,
    Situation,
    UserSession,
)
from africasignal.publish import accounts
from africasignal.publish.email import FakeProvider
from africasignal.publish.hooks import (
    clear_publication_hooks,
    notify_published,
    register_publication_hook,
)
from africasignal.publish.notify import queue_notifications
from africasignal.publish.outbox import dispatch_pending
from tests.integration.email_support import NOW, add_place, add_situation, add_user, add_version
from tests.integration.web.web_support import ORIGIN

SLUG = "price-pms"


@pytest.fixture
def situation(session: Session) -> Situation:
    country = add_place(session, "NG", "Nigeria", "country")
    state = add_place(session, "NG-LA", "Lagos", "state", country)
    situation = add_situation(session, SLUG, state)
    add_version(session, situation)
    return situation


def sent_token(session: Session) -> str:
    """Send the queued emails and return the raw token from the sign-in link."""
    provider = FakeProvider()
    dispatch_pending(session, provider, datetime.now(UTC) + timedelta(seconds=1))
    link = next(line for line in provider.sent[-1].text.splitlines() if line.startswith("http"))
    assert urlparse(link).path == "/signin/verify"
    return parse_qs(urlparse(link).query)["token"][0]


def sign_in(client: TestClient, session: Session, email: str = "ada@example.com") -> None:
    assert client.post("/signin", data={"email": email}, headers=ORIGIN).status_code == 200
    token = sent_token(session)
    response = client.post(
        "/signin/verify", data={"token": token}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303, response.text


# --- sign in ------------------------------------------------------------------------------------


def test_sign_in_with_an_emailed_link(client: TestClient, session: Session) -> None:
    page = client.post("/signin", data={"email": " Ada@Example.com "}, headers=ORIGIN)
    assert page.status_code == 200 and "Check your email" in page.text
    assert session.scalars(select(Outbox.kind)).one() == "email_login"

    token = sent_token(session)
    response = client.post(
        "/signin/verify", data={"token": token}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == "/account"
    cookie = response.headers["set-cookie"]
    assert "as_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie

    account = client.get("/account")
    assert account.status_code == 200 and "ada@example.com" in account.text
    assert account.headers["cache-control"] == "private, no-cache"
    assert session.scalars(select(AppUser.email_verified_at)).one() is not None


def test_a_mail_scanner_opening_the_link_does_not_use_it_up(
    client: TestClient, session: Session
) -> None:
    client.post("/signin", data={"email": "ada@example.com"}, headers=ORIGIN)
    token = sent_token(session)
    for _ in range(3):  # a scanner, then a preview, then the person's mail app
        page = client.get("/signin/verify", params={"token": token})
        assert page.status_code == 200 and "Press the button" in page.text
        # not no-referrer: see test_web_security
        assert page.headers["referrer-policy"] == "same-origin"
        assert page.headers["cache-control"] == "no-store"
    assert "as_session" not in page.headers.get("set-cookie", "")
    assert client.get("/account", follow_redirects=False).status_code == 303  # not signed in yet
    # The token is in the form body of the POST, not in a URL, and still works.
    response = client.post(
        "/signin/verify", data={"token": token}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert client.get("/account").status_code == 200


def test_a_link_works_once(client: TestClient, session: Session) -> None:
    client.post("/signin", data={"email": "ada@example.com"}, headers=ORIGIN)
    token = sent_token(session)
    assert client.post("/signin/verify", data={"token": token}, headers=ORIGIN).status_code == 200
    again = client.post("/signin/verify", data={"token": token}, headers=ORIGIN)
    assert again.status_code == 400 and "expired or was already used" in again.text
    bad = client.post("/signin/verify", data={"token": "x" * 40}, headers=ORIGIN)
    assert bad.status_code == 400
    assert client.get("/signin/verify", params={"token": "short"}).status_code == 400


def test_the_sign_in_answer_does_not_reveal_who_has_an_account(
    client: TestClient, session: Session
) -> None:
    add_user(session, "known@example.com")
    known = client.post("/signin", data={"email": "known@example.com"}, headers=ORIGIN)
    unknown = client.post("/signin", data={"email": "new@example.com"}, headers=ORIGIN)
    assert known.status_code == unknown.status_code == 200
    assert known.text == unknown.text
    # An address that is over its hourly limit gets the same answer and no extra email.
    for _ in range(6):
        client.post("/signin", data={"email": "known@example.com"}, headers=ORIGIN)
    over = client.post("/signin", data={"email": "known@example.com"}, headers=ORIGIN)
    assert over.status_code == 200 and "Check your email" in over.text
    assert session.scalar(select(func.count()).select_from(Outbox)) == 5 + 1  # the limit, and new


def test_bad_addresses_and_too_many_requests(client: TestClient) -> None:
    bad = client.post("/signin", data={"email": "not-an-email"}, headers=ORIGIN)
    assert bad.status_code == 400 and "valid email" in bad.text
    for i in range(9):  # the bad address above counted as the first of ten
        assert (
            client.post("/signin", data={"email": f"a{i}@example.com"}, headers=ORIGIN).status_code
            == 200
        )
    limited = client.post("/signin", data={"email": "z@example.com"}, headers=ORIGIN)
    assert limited.status_code == 429 and "Retry-After" in limited.headers


def test_signing_out_ends_the_session(client: TestClient, session: Session) -> None:
    sign_in(client, session)
    assert client.get("/account").status_code == 200
    response = client.post("/signout", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert session.scalars(select(UserSession.revoked_at)).one() is not None
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_sign_in_returns_the_reader_to_where_they_were(
    client: TestClient, session: Session, situation: Situation
) -> None:
    redirect = client.post(f"/s/{SLUG}/follow", headers=ORIGIN, follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers["location"] == "/signin"
    assert f"as_next={'/s/' + SLUG}" in redirect.headers["set-cookie"].replace('"', "")
    client.post("/signin", data={"email": "ada@example.com"}, headers=ORIGIN)
    token = sent_token(session)
    done = client.post(
        "/signin/verify", data={"token": token}, headers=ORIGIN, follow_redirects=False
    )
    assert done.headers["location"] == f"/s/{SLUG}"


# --- follows and notifications ------------------------------------------------------------------


def test_the_situation_page_offers_follow_feedback_and_report(
    client: TestClient, situation: Situation
) -> None:
    page = client.get(f"/s/{SLUG}").text
    assert f'action="/s/{SLUG}/follow"' in page and f'action="/s/{SLUG}/useful"' in page
    assert f'href="/s/{SLUG}/report"' in page
    assert "ada@example.com" not in page  # the cached page holds nothing personal


def test_follow_and_unfollow(client: TestClient, session: Session, situation: Situation) -> None:
    sign_in(client, session)
    user = session.scalars(select(AppUser)).one()
    response = client.post(f"/s/{SLUG}/follow", headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"] == "/following?notice=followed"
    client.post(f"/s/{SLUG}/follow", headers=ORIGIN)  # following twice changes nothing
    assert session.scalar(select(func.count()).select_from(Follow)) == 1
    events = session.scalars(select(Event.name).where(Event.user_id == user.id)).all()
    assert events.count("follow") == 1

    page = client.get("/following").text
    assert "You now follow" not in page  # the notice comes from the redirect's query string
    listed = client.get("/following?notice=followed").text
    assert "You now follow this situation." in listed
    assert f"/s/{SLUG}" in listed and "Unfollow" in listed

    client.post(f"/s/{SLUG}/unfollow", headers=ORIGIN)
    assert session.scalar(select(func.count()).select_from(Follow)) == 0
    assert "unfollow" in session.scalars(select(Event.name)).all()


def test_follow_needs_a_published_situation(client: TestClient, session: Session) -> None:
    sign_in(client, session)
    assert client.post("/s/nope/follow", headers=ORIGIN).status_code == 404
    add_situation(session, "unpublished", add_place(session, "NG-AB", "Abia", "state"))
    assert client.post("/s/unpublished/follow", headers=ORIGIN).status_code == 404


def test_the_following_page_needs_an_account(client: TestClient) -> None:
    for path in ("/following", "/account", "/account/export"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/signin"


def test_in_site_notifications_are_listed_and_marked_read(
    client: TestClient, session: Session, situation: Situation
) -> None:
    sign_in(client, session)
    user = session.scalars(select(AppUser)).one()
    accounts.follow(session, user.id, situation.id)
    v1 = situation.current_version_id
    assert v1 is not None
    session.add(
        Notification(user_id=user.id, assessment_version_id=v1, kind="new_version", dedupe_key="a")
    )
    cancelled = Notification(
        user_id=user.id,
        assessment_version_id=v1,
        kind="correction",
        dedupe_key="b",
        cancelled_at=NOW,
    )
    session.add(cancelled)
    session.flush()

    page = client.get("/following").text
    assert "New information" in page and "Correction" not in page  # cancelled ones are not shown
    client.post("/following/read", headers=ORIGIN)
    assert "Nothing new." in client.get("/following").text
    unread = select(func.count()).select_from(Notification).where(Notification.read_at.is_(None))
    assert session.scalar(unread.where(Notification.cancelled_at.is_(None))) == 0


def test_publishing_queues_follower_notifications(session: Session, situation: Situation) -> None:
    """Publishing a version queues the job that notifies its followers."""
    clear_publication_hooks()
    register_publication_hook(queue_notifications)
    try:
        assert situation.current_version_id is not None
        notify_published(session, situation.current_version_id, "new_version")
    finally:
        clear_publication_hooks()
    job = session.scalars(select(Job).where(Job.kind == "notify_followers")).one()
    assert job.payload == {"version_id": situation.current_version_id, "kind": "new_version"}


# --- the account page ---------------------------------------------------------------------------


def test_digest_choice_and_preferences(
    client: TestClient, session: Session, situation: Situation
) -> None:
    sign_in(client, session)
    user = session.scalars(select(AppUser)).one()
    client.post("/account/digest", data={"digest": "on"}, headers=ORIGIN)
    session.refresh(user)
    assert user.digest_opt_in and user.digest_opt_in_at is not None
    assert "weekly email is <strong>on</strong>" in client.get("/account").text
    client.post("/account/digest", data={"digest": "off"}, headers=ORIGIN)
    session.refresh(user)
    assert not user.digest_opt_in

    lagos = session.scalars(select(Place.id).where(Place.code == "NG-LA")).one()
    client.post(
        "/account/preferences",
        data={"place": ["NG-LA", "NOPE"], "topic": ["energy", "weather"]},
        headers=ORIGIN,
    )
    preference = session.get(Preference, user.id)
    assert preference is not None
    assert preference.place_ids == [lagos] and preference.topics == ["energy"]
    page = client.get("/account").text
    assert 'value="NG-LA" checked' in page and 'value="energy" checked' in page


def test_export_returns_the_readers_data(
    client: TestClient, session: Session, situation: Situation
) -> None:
    sign_in(client, session)
    user = session.scalars(select(AppUser)).one()
    accounts.follow(session, user.id, situation.id)
    response = client.get("/account/export")
    assert response.headers["content-disposition"].startswith("attachment")
    data = response.json()
    assert data["email"] == "ada@example.com"
    assert data["follows"][0]["situation"] == SLUG


def test_deleting_the_account_needs_the_word_and_removes_everything(
    client: TestClient, session: Session, situation: Situation
) -> None:
    sign_in(client, session)
    user = session.scalars(select(AppUser)).one()
    accounts.follow(session, user.id, situation.id)
    refused = client.post("/account/delete", data={"confirm": "yes"}, headers=ORIGIN)
    assert refused.status_code == 400 and session.get(AppUser, user.id) is not None

    done = client.post("/account/delete", data={"confirm": "Delete"}, headers=ORIGIN)
    assert done.status_code == 200 and "account is deleted" in done.text
    assert session.scalar(select(func.count()).select_from(AppUser)) == 0
    assert session.scalar(select(func.count()).select_from(Follow)) == 0
    assert session.scalar(select(func.count()).select_from(UserSession)) == 0
    assert client.get("/account", follow_redirects=False).status_code == 303


# --- unsubscribe --------------------------------------------------------------------------------


def unsubscribe_link(user: AppUser) -> str:
    return f"/unsubscribe?t={accounts.unsubscribe_token(user.id)}"


def test_opening_the_unsubscribe_link_changes_nothing(client: TestClient, session: Session) -> None:
    user = add_user(session, "ada@example.com", digest=True)
    page = client.get(unsubscribe_link(user))
    assert page.status_code == 200 and "Stop the weekly email?" in page.text
    session.refresh(user)
    assert user.digest_opt_in  # a mail scanner opened it


def test_the_button_unsubscribes(client: TestClient, session: Session) -> None:
    user = add_user(session, "ada@example.com", digest=True)
    response = client.post(unsubscribe_link(user), headers=ORIGIN)
    assert response.status_code == 200 and "You are unsubscribed" in response.text
    session.refresh(user)
    assert not user.digest_opt_in


def test_one_click_unsubscribe_from_a_mail_client(client: TestClient, session: Session) -> None:
    """RFC 8058: the mail client POSTs the link's address with this body and no Origin header."""
    user = add_user(session, "ada@example.com", digest=True)
    response = client.post(
        unsubscribe_link(user),
        content="List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    session.refresh(user)
    assert not user.digest_opt_in
    assert client.post(unsubscribe_link(user)).status_code == 200  # repeating it is harmless


def test_a_forged_unsubscribe_token_does_nothing(client: TestClient, session: Session) -> None:
    user = add_user(session, "ada@example.com", digest=True)
    assert client.post(f"/unsubscribe?t={user.id}.forged").status_code == 400
    assert client.post("/unsubscribe").status_code == 400
    assert client.get("/unsubscribe?t=x").status_code == 400
    session.refresh(user)
    assert user.digest_opt_in


# --- cross-site requests ------------------------------------------------------------------------


def test_forms_from_other_sites_are_refused(
    client: TestClient, session: Session, situation: Situation
) -> None:
    sign_in(client, session)
    for headers in ({}, {"Origin": "https://evil.example"}, {"Origin": "null"}):
        response = client.post(f"/s/{SLUG}/follow", headers=headers, follow_redirects=False)
        assert response.status_code == 403, headers
        assert (
            client.post("/account/delete", data={"confirm": "delete"}, headers=headers).status_code
            == 403
        )
    assert session.scalar(select(func.count()).select_from(Follow)) == 0
    assert session.scalar(select(func.count()).select_from(AppUser)) == 1
    # The Referer is used when a browser sends no Origin.
    ok = client.post(
        f"/s/{SLUG}/follow",
        headers={"Referer": f"http://testserver/s/{SLUG}"},
        follow_redirects=False,
    )
    assert ok.status_code == 303


def test_the_public_address_is_trusted_behind_a_tls_proxy(
    client: TestClient, situation: Situation, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser's origin is the public address, not the http address the app sees."""
    sign_in(client, session)
    https = {"Origin": "https://africasignal.example"}
    refused = client.post(f"/s/{SLUG}/follow", headers=https, follow_redirects=False)
    assert refused.status_code == 403
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://africasignal.example")
    accepted = client.post(f"/s/{SLUG}/follow", headers=https, follow_redirects=False)
    assert accepted.status_code == 303


def test_the_worker_registers_the_hook_and_the_new_job_when_it_loads_its_handlers() -> None:
    """Run apart from the tests, so importing the handlers does not leak into other tests."""
    code = (
        "from africasignal.jobs.handlers import HANDLERS, load_all\n"
        "from africasignal.publish import hooks\n"
        "load_all()\n"
        "assert len(hooks._hooks) == 1, hooks._hooks\n"
        "assert {'notify_followers', 'dispatch_outbox', 'prune_events'} <= set(HANDLERS)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
