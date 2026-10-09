"""Small social publishing clients with conservative outcome classification."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class SocialRejected(ValueError):
    """A provider definitively refused the publication request."""


class SocialOutcomeUnknown(RuntimeError):
    """A provider may have accepted the request; callers must not retry automatically."""


@dataclass(frozen=True)
class Receipt:
    external_id: str


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(30.0, connect=5.0),
        follow_redirects=False,
        trust_env=False,
    )


def _check_status(response: httpx.Response, platform: str) -> None:
    if 400 <= response.status_code < 500:
        raise SocialRejected(f"{platform} rejected the post (HTTP {response.status_code})")
    if response.status_code < 200 or response.status_code >= 300:
        raise SocialOutcomeUnknown(
            f"{platform} returned HTTP {response.status_code}; confirm the account before retrying"
        )


def _json(response: httpx.Response, platform: str) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError:
        raise SocialOutcomeUnknown(
            f"{platform} returned an unreadable response; confirm the account before retrying"
        ) from None
    if not isinstance(payload, dict):
        raise SocialOutcomeUnknown(f"{platform} returned an invalid response")
    return payload


def _id(payload: dict[str, object], platform: str, *path: str) -> str:
    value: object = payload
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    if not isinstance(value, (str, int)) or not str(value):
        raise SocialOutcomeUnknown(
            f"{platform} returned no post identifier; confirm the account before retrying"
        )
    return str(value)


def create_facebook_post(page_id: str, access_token: str, api_version: str, text: str) -> Receipt:
    url = f"https://graph.facebook.com/{api_version}/{page_id}/feed"
    try:
        with _client() as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                data={"message": text},
            )
    except httpx.HTTPError as exc:
        raise SocialOutcomeUnknown(
            "Facebook post outcome is unknown; automatic retry is disabled"
        ) from exc
    _check_status(response, "Facebook")
    payload = _json(response, "Facebook")
    if "error" in payload:
        raise SocialRejected("Facebook rejected the post")
    return Receipt(_id(payload, "Facebook", "id"))


def create_instagram_post(
    account_id: str,
    access_token: str,
    api_version: str,
    caption: str,
    image_url: str,
) -> Receipt:
    """Create and publish one generated image as an Instagram feed post."""
    if urlsplit(image_url).scheme != "https" or not urlsplit(image_url).hostname:
        raise SocialRejected("Instagram media must use a public HTTPS address")
    base = f"https://graph.facebook.com/{api_version}/{account_id}"
    try:
        with _client() as client:
            container = client.post(
                f"{base}/media",
                headers={"Authorization": f"Bearer {access_token}"},
                data={"image_url": image_url, "caption": caption},
            )
            _check_status(container, "Instagram")
            created = _json(container, "Instagram")
            if "error" in created:
                raise SocialRejected("Instagram rejected media creation")
            creation_id = _id(created, "Instagram", "id")
            published = client.post(
                f"{base}/media_publish",
                headers={"Authorization": f"Bearer {access_token}"},
                data={"creation_id": creation_id},
            )
    except SocialRejected:
        raise
    except SocialOutcomeUnknown:
        raise
    except httpx.HTTPError as exc:
        raise SocialOutcomeUnknown(
            "Instagram publication outcome is unknown; automatic retry is disabled"
        ) from exc
    _check_status(published, "Instagram")
    payload = _json(published, "Instagram")
    if "error" in payload:
        raise SocialRejected("Instagram rejected the media publication")
    return Receipt(_id(payload, "Instagram", "id"))


def create_telegram_post(bot_token: str, channel_id: str, text: str) -> Receipt:
    """Send an approved channel post through Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        with _client() as client:
            response = client.post(url, json={"chat_id": channel_id, "text": text})
    except httpx.HTTPError as exc:
        raise SocialOutcomeUnknown(
            "Telegram post outcome is unknown; automatic retry is disabled"
        ) from exc
    _check_status(response, "Telegram")
    payload = _json(response, "Telegram")
    if payload.get("ok") is not True:
        raise SocialRejected("Telegram rejected the channel post")
    return Receipt(_id(payload, "Telegram", "result", "message_id"))


def upload_youtube_video(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    expected_channel_id: str,
    title: str,
    description: str,
    category_id: str,
    privacy_status: str,
    video: bytes,
) -> Receipt:
    """Upload an operator-supplied MP4 using YouTube's resumable upload protocol."""
    if not video or privacy_status not in {"private", "unlisted", "public"}:
        raise SocialRejected("YouTube upload details are invalid")
    try:
        with _client() as client:
            token_response = client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            _check_status(token_response, "Google OAuth")
            token_payload = _json(token_response, "Google OAuth")
            access_token = token_payload.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise SocialRejected("Google OAuth did not return an access token")

            channels = client.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "id", "mine": "true"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            _check_status(channels, "YouTube")
            channel_payload = _json(channels, "YouTube")
            ids = channel_payload.get("items")
            actual_channel = (
                ids[0].get("id")
                if isinstance(ids, list) and ids and isinstance(ids[0], dict)
                else None
            )
            if actual_channel != expected_channel_id:
                raise SocialRejected(
                    "The authorized YouTube channel does not match the configured ID"
                )

            metadata = {
                "snippet": {
                    "title": title[:100],
                    "description": description[:5000],
                    "categoryId": category_id,
                },
                "status": {"privacyStatus": privacy_status},
            }
            start = client.post(
                "https://www.googleapis.com/upload/youtube/v3/videos",
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-Upload-Content-Type": "video/mp4",
                    "X-Upload-Content-Length": str(len(video)),
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json=metadata,
            )
            _check_status(start, "YouTube")
            location = start.headers.get("location", "")
            parsed = urlsplit(location)
            if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com":
                raise SocialOutcomeUnknown("YouTube returned an invalid upload session")
            uploaded = client.put(
                location,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(video)),
                },
                content=video,
            )
    except (SocialRejected, SocialOutcomeUnknown):
        raise
    except httpx.HTTPError as exc:
        raise SocialOutcomeUnknown(
            "YouTube upload outcome is unknown; automatic retry is disabled"
        ) from exc
    _check_status(uploaded, "YouTube")
    payload = _json(uploaded, "YouTube")
    if "error" in payload:
        raise SocialRejected("YouTube rejected the video upload")
    return Receipt(_id(payload, "YouTube", "id"))
