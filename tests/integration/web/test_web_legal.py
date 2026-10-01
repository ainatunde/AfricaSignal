"""The legal pages (AS-043): they say what the code does, name the operator and contact from the
console settings, and keep their draft banner until a person confirms a legal review."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from africasignal import metrics, operators, settings_store
from africasignal.publish.login_tokens import LOGIN_TOKEN_TTL, SESSION_TTL
from africasignal.web.analytics import SESSION_COOKIE
from africasignal.web.routes import legal, public
from africasignal.web.user_dep import NEXT_COOKIE
from tests.integration.email_support import add_place, add_situation

BROWSER = {"User-Agent": "Mozilla/5.0"}
PAGES = ("/privacy", "/terms", "/corrections")
_TAGS = re.compile(r"<[^>]+>")


def words(html: str) -> str:
    body = html.split("<main", 1)[-1].split("</main>", 1)[0]
    return re.sub(r"\s+", " ", _TAGS.sub(" ", body))


@pytest.fixture
def admin(session: Session):  # type: ignore[no-untyped-def]
    return operators.create_operator(session, "ops@example.org", "correct horse battery", "admin")[
        0
    ]


@pytest.mark.parametrize("path", PAGES)
def test_each_page_is_a_marked_draft_until_a_review_is_confirmed(
    client: TestClient, session: Session, admin, path: str
) -> None:  # type: ignore[no-untyped-def]
    page = client.get(path, headers=BROWSER).text
    assert "data-legal-draft" in page
    assert "Draft, not yet reviewed by a lawyer" in page
    assert '<meta name="robots" content="noindex, nofollow">' in page

    settings_store.set_value(session, admin, "legal_review_confirmed", "yes")
    page = client.get(path, headers=BROWSER).text
    assert "data-legal-draft" not in page
    assert "noindex" not in page


@pytest.mark.parametrize("path", PAGES)
def test_the_operator_and_contact_come_from_the_settings(
    client: TestClient, session: Session, admin, path: str
) -> None:  # type: ignore[no-untyped-def]
    page = client.get(path, headers=BROWSER).text
    assert "[contact address not set yet]" in page

    settings_store.set_value(session, admin, "contact_email", "privacy@africasignal.example")
    settings_store.set_value(session, admin, "operator_name", "Example Ltd")
    page = client.get(path, headers=BROWSER).text
    assert 'href="mailto:privacy@africasignal.example"' in page
    assert "[contact address not set yet]" not in page
    if path != "/corrections":
        assert "Example Ltd" in page


def test_open_questions_stay_visible_after_the_banner_is_gone(
    client: TestClient, session: Session, admin
) -> None:  # type: ignore[no-untyped-def]
    settings_store.set_value(session, admin, "legal_review_confirmed", "yes")
    for path in PAGES:
        assert "data-todo" in client.get(path, headers=BROWSER).text  # the checklist greps for it


def test_the_privacy_page_lists_every_cookie_the_site_sets(
    client: TestClient, session: Session
) -> None:
    add_situation(session, "price-pms", add_place(session, "NG-LA", "Lagos", "state"))
    listed = set(
        re.findall(r"<code>([a-z_]+)</code>", client.get("/privacy", headers=BROWSER).text)
    )
    constants = {
        public.PLACE_COOKIE,
        public.VISIT_PREV,
        public.VISIT_CUR,
        metrics.ANON_COOKIE,
        SESSION_COOKIE,
        NEXT_COOKIE,
    }
    assert constants <= listed
    assert {c["name"] for c in legal.cookies()} == constants

    # Cookies really set by the pages are all on the list.
    client.get("/?place=NG-LA", headers=BROWSER, follow_redirects=True)
    client.get("/", headers=BROWSER)
    client.post(
        "/signin", data={"email": "reader@example.org", "next": "/following"}, headers=BROWSER
    )
    assert client.cookies, "the test should have received some cookies"
    assert set(client.cookies.keys()) <= constants


def test_the_privacy_page_states_the_retention_the_code_applies(
    client: TestClient,
    session: Session,
    admin,  # type: ignore[no-untyped-def]
) -> None:
    text = words(client.get("/privacy", headers=BROWSER).text)
    assert "13 months" in text and metrics.RETENTION.days // 31 == 13
    assert f"valid for {int(LOGIN_TOKEN_TTL.total_seconds() // 60)} minutes" in text
    assert f"{SESSION_TTL.days} days" in text
    assert "30 days" in text  # backups, the default

    settings_store.set_value(session, admin, "backup_retain_days", "14")
    assert "backups for 14 days" in words(client.get("/privacy", headers=BROWSER).text)


def test_the_privacy_page_states_the_retention_jobs_the_code_runs(
    client: TestClient,
    session: Session,
    admin,  # type: ignore[no-untyped-def]
) -> None:
    from africasignal.publish import deletions, retention

    text = words(client.get("/privacy", headers=BROWSER).text)
    assert f"{retention.feedback_retention_months(session)} months" in text  # default 24
    assert f"deleted {retention.UNVERIFIED_ACCOUNT_TTL.days} days later" in text
    assert f"Deleted {retention.TOKEN_GRACE.days} days after the link or session stops" in text
    assert f"for {30 + deletions.LEDGER_MARGIN.days} days" in text  # the ledger, default backups
    assert "page-view records that were recorded while you were signed in" in text

    settings_store.set_value(session, admin, "feedback_retention_months", "12")
    settings_store.set_value(session, admin, "backup_retain_days", "14")
    text = words(client.get("/privacy", headers=BROWSER).text)
    assert "12 months. After that the text" in text
    assert f"for {14 + deletions.LEDGER_MARGIN.days} days" in text


def test_the_privacy_page_no_longer_carries_the_open_items_the_code_now_answers(
    client: TestClient,
) -> None:
    page = client.get("/privacy", headers=BROWSER).text
    assert "fixed retention period for feedback" not in page
    assert "how deletions are applied again" not in page


def test_the_privacy_page_names_the_email_provider_once_chosen(
    client: TestClient,
    session: Session,
    admin,  # type: ignore[no-untyped-def]
) -> None:
    assert "(currently" not in client.get("/privacy", headers=BROWSER).text
    settings_store.set_value(session, admin, "email_provider", "postmark")
    assert "(currently Postmark)" in words(client.get("/privacy", headers=BROWSER).text)


def test_every_page_links_to_the_legal_pages_in_the_footer(client: TestClient) -> None:
    footer = client.get("/explore", headers=BROWSER).text.split("<footer", 1)[1]
    for path in PAGES:
        assert f'href="{path}"' in footer


def test_the_correction_policy_states_the_response_target(client: TestClient) -> None:
    text = words(client.get("/corrections", headers=BROWSER).text)
    assert f"within {legal.ERROR_REPORT_RESPONSE_HOURS} hours" in text
    assert legal.ERROR_REPORT_RESPONSE_HOURS == 72  # plan D3


def test_the_crawler_page_falls_back_to_the_contact_address(
    client: TestClient,
    session: Session,
    admin,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.delenv("BOT_CONTACT_EMAIL", raising=False)
    settings_store.set_value(session, admin, "contact_email", "hello@africasignal.example")
    assert "hello@africasignal.example" in client.get("/about/bot", headers=BROWSER).text
    monkeypatch.setenv("BOT_CONTACT_EMAIL", "bots@example.org")
    assert "bots@example.org" in client.get("/about/bot", headers=BROWSER).text
