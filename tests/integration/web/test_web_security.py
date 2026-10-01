"""Findings of the AS-042 security review that have a code fix: browser security headers, limits
that a script cannot dodge by dropping its cookie, and links that are never ``javascript:``."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import EvidenceDocument, Feedback, Situation, Source
from africasignal.storage import S3Store
from africasignal.web.routes import feedback, public
from tests.integration.email_support import add_place, add_situation, add_version
from tests.integration.web.web_support import ORIGIN, seed_petrol

SLUG = "price-pms"


@pytest.fixture
def situation(session: Session) -> Situation:
    country = add_place(session, "NG", "Nigeria", "country")
    state = add_place(session, "NG-LA", "Lagos", "state", country)
    situation = add_situation(session, SLUG, state)
    add_version(session, situation)
    return situation


# --- security headers ---------------------------------------------------------------------------


def test_public_pages_carry_a_strict_content_security_policy(client: TestClient) -> None:
    response = client.get("/about/method")
    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy and "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy and "unsafe-inline" not in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "strict-origin" in response.headers["referrer-policy"]


def test_error_pages_and_the_json_api_get_the_headers_too(client: TestClient) -> None:
    for path in ("/no-such-page", "/v1/coverage", "/healthz"):
        response = client.get(path)
        assert response.headers["x-content-type-options"] == "nosniff", path
        assert "content-security-policy" in response.headers, path


def test_a_refused_cross_origin_post_still_gets_the_headers(client: TestClient) -> None:
    response = client.post("/admin/login", data={}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.headers["x-frame-options"] == "DENY"


def test_the_consoles_own_headers_are_not_replaced(client: TestClient) -> None:
    response = client.get("/admin/login")
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    # Never "no-referrer": browsers then send "Origin: null" on a same-origin form post, which the
    # origin guard refuses, so the sign-in form would not work in a real browser.
    assert response.headers["referrer-policy"] == "same-origin"


def test_hsts_is_sent_outside_development_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "strict-transport-security" not in client.get("/about/method").headers
    monkeypatch.setenv("ENV", "staging")
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-that-is-long-enough")
    from africasignal.config import get_settings

    get_settings.cache_clear()
    try:
        assert "max-age=" in client.get("/about/method").headers["strict-transport-security"]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# --- limits that do not depend on the visitor cookie --------------------------------------------


def test_feedback_is_limited_per_client_even_when_the_cookie_is_dropped(
    client: TestClient, session: Session, situation: Situation, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        feedback, "feedback_limiter", feedback.RateLimiter(limit=3, window_seconds=3600)
    )
    codes = []
    for n in range(6):
        client.cookies.clear()  # a script that never keeps a cookie gets a new visitor code each time
        response = client.post(
            f"/s/{SLUG}/report", data={"text": f"something is wrong {n}"}, headers=ORIGIN
        )
        codes.append(response.status_code)
    assert codes == [200, 200, 200, 429, 429, 429]  # 200 after following the redirect
    assert len(session.scalars(select(Feedback)).all()) == 3


def test_useful_answers_share_that_limit(
    client: TestClient, situation: Situation, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        feedback, "feedback_limiter", feedback.RateLimiter(limit=1, window_seconds=3600)
    )
    client.cookies.clear()
    assert (
        client.post(f"/s/{SLUG}/useful", data={"answer": "yes"}, headers=ORIGIN).status_code == 200
    )
    client.cookies.clear()
    assert (
        client.post(f"/s/{SLUG}/useful", data={"answer": "yes"}, headers=ORIGIN).status_code == 429
    )


def test_locating_a_position_is_rate_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(public, "locate_limiter", public.RateLimiter(limit=2, window_seconds=60))
    body = {"lat": 6.6, "lon": 3.3}
    assert client.post("/places/locate", json=body).status_code == 200
    assert client.post("/places/locate", json=body).status_code == 200
    refused = client.post("/places/locate", json=body)
    assert refused.status_code == 429
    assert refused.headers["retry-after"].isdigit()
    assert refused.headers["cache-control"] == "no-store"


# --- evidence links -----------------------------------------------------------------------------


def test_an_evidence_address_that_is_not_http_is_never_a_link(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    _, version = seed_petrol(session, store, source)["NG-LA"]
    doc = session.get(EvidenceDocument, version.facts[0]["evidence_ids"][0])
    assert doc is not None
    doc.url = "javascript:alert(document.domain)"
    session.flush()
    public.clear_page_cache()
    page = client.get("/s/price-pms_litre-ng-la").text
    assert "javascript:" not in page
    api = client.get("/v1/situations/price-pms_litre-ng-la").json()
    assert all(not e["url"].lower().startswith("javascript:") for e in api["evidence"])


def test_a_normal_evidence_address_is_still_a_link(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    _, version = seed_petrol(session, store, source)["NG-LA"]
    doc = session.get(EvidenceDocument, version.facts[0]["evidence_ids"][0])
    assert doc is not None
    page = client.get("/s/price-pms_litre-ng-la").text
    assert f'href="{doc.url}"' in page
