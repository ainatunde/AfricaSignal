"""The public address comes from the operator console on every use (settings_store), so a change
there shows up in channel posts and share links without a restart."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from africasignal import settings_store
from africasignal.models import Operator, Source
from africasignal.publish.whatsapp_text import PostError, channel_posts
from africasignal.storage import S3Store
from tests.integration.web.web_support import seed_petrol

SINCE = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_env_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


@pytest.fixture
def operator(session: Session) -> Operator:
    op = Operator(email="ops@example.org", password_hash="x", totp_secret_enc="x", role="admin")
    session.add(op)
    session.flush()
    return op


def test_channel_posts_follow_the_console_value_each_time(
    session: Session, store: S3Store, source: Source, operator: Operator
) -> None:
    seeded = seed_petrol(session, store, source)
    seeded["NG-LA"][1].severity = "medium"
    seeded["NG"][1].severity = "none"  # only Lagos is a material change
    session.flush()
    with pytest.raises(PostError, match="public address is not set"):
        channel_posts(session, SINCE)
    settings_store.set_value(session, operator, "public_base_url", "https://signal.example")
    (post,) = channel_posts(session, SINCE)
    assert post.whatsapp.link == "https://signal.example/s/price-pms_litre-ng-la?ref=wa"
    assert post.x.text.endswith("https://signal.example/s/price-pms_litre-ng-la?ref=x")
    settings_store.set_value(session, operator, "public_base_url", "https://other.example")
    (post,) = channel_posts(session, SINCE)
    assert post.whatsapp.link.startswith("https://other.example/s/")  # no restart, no cache
    (post,) = channel_posts(session, SINCE, base_url="https://override.example")
    assert post.whatsapp.link.startswith("https://override.example/")


def test_the_environment_is_the_fallback_until_the_console_sets_a_value(
    session: Session,
    store: S3Store,
    source: Source,
    operator: Operator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = seed_petrol(session, store, source)
    seeded["NG-LA"][1].severity = "medium"
    seeded["NG"][1].severity = "none"  # only Lagos is a material change
    session.flush()
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://from-env.example")
    (post,) = channel_posts(session, SINCE)
    assert post.whatsapp.link.startswith("https://from-env.example/")
    settings_store.set_value(session, operator, "public_base_url", "https://console.example")
    (post,) = channel_posts(session, SINCE)
    assert post.whatsapp.link.startswith("https://console.example/")
    settings_store.clear_value(session, operator, "public_base_url")
    (post,) = channel_posts(session, SINCE)
    assert post.whatsapp.link.startswith("https://from-env.example/")


def test_the_share_link_follows_the_console_value_despite_the_page_cache(
    session: Session, store: S3Store, source: Source, operator: Operator, client: TestClient
) -> None:
    seed_petrol(session, store, source)
    assert (
        'data-share="/s/price-pms_litre-ng-la?ref=share"'
        in client.get("/s/price-pms_litre-ng-la").text
    )  # nothing set: the link stays relative
    settings_store.set_value(session, operator, "public_base_url", "https://signal.example")
    page = client.get("/s/price-pms_litre-ng-la").text
    assert 'data-share="https://signal.example/s/price-pms_litre-ng-la?ref=share"' in page
    settings_store.set_value(session, operator, "public_base_url", "https://new.example")
    page = client.get("/s/price-pms_litre-ng-la").text
    assert 'data-share="https://new.example/s/price-pms_litre-ng-la?ref=share"' in page
