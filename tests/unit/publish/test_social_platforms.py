from __future__ import annotations

import httpx
import pytest

from africasignal.publish import social_platforms as platforms


def mock_client(monkeypatch: pytest.MonkeyPatch, handler):
    monkeypatch.setattr(
        platforms,
        "_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
    )


def test_facebook_returns_receipt_without_exposing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "graph.facebook.com"
        assert request.url.path.endswith("/page/feed")
        assert "secret" not in str(request.url)
        return httpx.Response(200, json={"id": "page_123"})

    mock_client(monkeypatch, handler)
    receipt = platforms.create_facebook_post("page", "secret", "v26.0", "hello")
    assert receipt.external_id == "page_123"


def test_instagram_creates_then_publishes_media(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/media"):
            assert request.url.scheme == "https"
            return httpx.Response(200, json={"id": "container"})
        return httpx.Response(200, json={"id": "media_post"})

    mock_client(monkeypatch, handler)
    receipt = platforms.create_instagram_post(
        "account", "token", "v26.0", "caption", "https://example.org/image"
    )
    assert receipt.external_id == "media_post"
    assert calls == ["/v26.0/account/media", "/v26.0/account/media_publish"]


def test_telegram_rejected_result_is_not_reported_as_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_client(
        monkeypatch,
        lambda _: httpx.Response(200, json={"ok": False, "description": "bad"}),
    )
    with pytest.raises(platforms.SocialRejected):
        platforms.create_telegram_post("bot-token", "@channel", "hello")


def test_youtube_checks_channel_before_resumable_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access"})
        if request.url.path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"id": "expected"}]})
        if request.url.path.endswith("/videos"):
            return httpx.Response(
                200, headers={"location": "https://www.googleapis.com/upload/session"}
            )
        return httpx.Response(200, json={"id": "video_id"})

    mock_client(monkeypatch, handler)
    receipt = platforms.upload_youtube_video(
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        expected_channel_id="expected",
        title="title",
        description="description",
        category_id="25",
        privacy_status="unlisted",
        video=b"0000ftypvideo",
    )
    assert receipt.external_id == "video_id"
    assert calls[-1] == "/upload/session"


def test_youtube_refuses_wrong_authorized_channel_before_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access"})
        return httpx.Response(200, json={"items": [{"id": "other"}]})

    mock_client(monkeypatch, handler)
    with pytest.raises(platforms.SocialRejected, match="does not match"):
        platforms.upload_youtube_video(
            client_id="id",
            client_secret="secret",
            refresh_token="refresh",
            expected_channel_id="expected",
            title="title",
            description="description",
            category_id="25",
            privacy_status="public",
            video=b"0000ftypvideo",
        )
    assert not any(path.endswith("/videos") for path in calls)
