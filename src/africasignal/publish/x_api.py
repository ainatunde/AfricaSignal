"""Minimal X v2 user-context client for one-time text publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

POSTS_URL = "https://api.x.com/2/tweets"
USERS_URL = "https://api.x.com/2/users"
POST_LOOKUP_URL = "https://api.x.com/2/tweets"


class XPostRejected(ValueError):
    """X returned a response that definitively rejected the post."""


class XPostOutcomeUnknown(RuntimeError):
    """The request may have reached X; callers must not retry it automatically."""


@dataclass(frozen=True)
class XPostReceipt:
    post_id: str


@dataclass(frozen=True)
class XMention:
    post_id: str
    conversation_id: str | None
    author_id: str
    created_at: datetime | None


@dataclass(frozen=True)
class XMentionsResult:
    mentions: tuple[XMention, ...]
    fetched_count: int
    newest_id: str | None
    next_token: str | None


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(10.0, connect=3.0),
        follow_redirects=False,
        trust_env=False,
    )


def _json_response(response: httpx.Response, action: str) -> dict[str, object]:
    if 400 <= response.status_code < 500:
        raise XPostRejected(f"X rejected the {action} (HTTP {response.status_code})")
    if response.status_code < 200 or response.status_code >= 300:
        raise XPostOutcomeUnknown(
            f"X returned HTTP {response.status_code}; the {action} outcome is unknown"
        )
    try:
        data = response.json()
    except ValueError:
        raise XPostOutcomeUnknown(
            f"X returned an invalid {action} response; automatic retry is disabled"
        ) from None
    if not isinstance(data, dict):
        raise XPostOutcomeUnknown(
            f"X returned an invalid {action} response; automatic retry is disabled"
        )
    return data


def _post(access_token: str, text: str, *, reply_to_post_id: str | None) -> XPostReceipt:
    if not text.strip():
        raise XPostRejected("X post text is empty")
    body: dict[str, object] = {"text": text}
    if reply_to_post_id is not None:
        if not reply_to_post_id.isdigit() or len(reply_to_post_id) > 19:
            raise XPostRejected("X reply target is invalid")
        body["reply"] = {"in_reply_to_tweet_id": reply_to_post_id}
    try:
        with _client() as client:
            response = client.post(
                POSTS_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise XPostOutcomeUnknown(
            "X request outcome is unknown; automatic retry is disabled"
        ) from exc

    payload = _json_response(response, "reply" if reply_to_post_id else "post")
    try:
        post_id = payload["data"]["id"]  # type: ignore[index]
    except (KeyError, TypeError):
        raise XPostOutcomeUnknown(
            "X accepted a response without a usable post id; automatic retry is disabled"
        ) from None
    if not isinstance(post_id, str) or not post_id.isdigit():
        raise XPostOutcomeUnknown("X returned an invalid post id; automatic retry is disabled")
    return XPostReceipt(post_id=post_id)


def create_post(access_token: str, text: str) -> XPostReceipt:
    """Create one text post using an OAuth 2.0 user-context access token.

    The client disables redirects and ambient proxy configuration. A transport error, 5xx, or
    malformed success response is ambiguous and must be reconciled by an operator, never retried.
    """
    return _post(access_token, text, reply_to_post_id=None)


def create_reply(access_token: str, text: str, reply_to_post_id: str) -> XPostReceipt:
    """Create a reply to the exact numeric X post selected from the authenticated mentions feed."""
    return _post(access_token, text, reply_to_post_id=reply_to_post_id)


def authenticated_user_id(access_token: str) -> str:
    """Resolve the user bound to an OAuth user token before publishing or reading mentions."""
    try:
        with _client() as client:
            response = client.get(
                f"{USERS_URL}/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise XPostOutcomeUnknown("X account identity could not be verified") from exc
    payload = _json_response(response, "account identity lookup")
    try:
        user_id = payload["data"]["id"]  # type: ignore[index]
    except (KeyError, TypeError):
        raise XPostOutcomeUnknown("X account identity response was invalid") from None
    if not isinstance(user_id, str) or not user_id.isdigit() or len(user_id) > 19:
        raise XPostOutcomeUnknown("X account identity response was invalid")
    return user_id


def get_mentions(
    access_token: str,
    account_user_id: str,
    max_results: int,
    since_id: str | None,
    page_token: str | None = None,
) -> XMentionsResult:
    """Read mentions for one account and discard all post text/profile data in memory."""
    if not account_user_id.isdigit() or len(account_user_id) > 19:
        raise XPostRejected("X account user ID is invalid")
    if not 5 <= max_results <= 100:
        raise XPostRejected("X mention read size must be between 5 and 100")
    if since_id is not None and (not since_id.isdigit() or len(since_id) > 19):
        raise XPostRejected("X mention cursor is invalid")
    if page_token is not None and (not page_token or len(page_token) > 4096):
        raise XPostRejected("X mention pagination cursor is invalid")
    params = {
        "max_results": str(max_results),
        "post.fields": "id,conversation_id,author_id,created_at",
    }
    if since_id:
        params["since_id"] = since_id
    if page_token:
        params["pagination_token"] = page_token
    try:
        with _client() as client:
            response = client.get(
                f"{USERS_URL}/{account_user_id}/mentions",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
    except httpx.HTTPError as exc:
        raise XPostOutcomeUnknown(
            "X mention read outcome is unknown; its full result allowance remains reserved"
        ) from exc
    payload = _json_response(response, "mention read")
    raw_mentions = payload.get("data", [])
    if not isinstance(raw_mentions, list) or len(raw_mentions) > max_results:
        raise XPostOutcomeUnknown("X returned an invalid mentions response")
    mentions: list[XMention] = []
    for item in raw_mentions:
        if not isinstance(item, dict):
            raise XPostOutcomeUnknown("X returned an invalid mention record")
        post_id, author_id = item.get("id"), item.get("author_id")
        conversation_id = item.get("conversation_id")
        if (
            not isinstance(post_id, str)
            or not post_id.isdigit()
            or len(post_id) > 19
            or not isinstance(author_id, str)
            or not author_id.isdigit()
            or len(author_id) > 19
            or (
                conversation_id is not None
                and (
                    not isinstance(conversation_id, str)
                    or not conversation_id.isdigit()
                    or len(conversation_id) > 19
                )
            )
        ):
            raise XPostOutcomeUnknown("X returned an invalid mention record")
        raw_created = item.get("created_at")
        created_at = None
        if raw_created is not None:
            if not isinstance(raw_created, str):
                raise XPostOutcomeUnknown("X returned an invalid mention timestamp")
            try:
                created_at = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
            except ValueError:
                raise XPostOutcomeUnknown("X returned an invalid mention timestamp") from None
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
        mentions.append(
            XMention(
                post_id=post_id,
                conversation_id=conversation_id,
                author_id=author_id,
                created_at=created_at,
            )
        )
    meta = payload.get("meta", {})
    newest_id = meta.get("newest_id") if isinstance(meta, dict) else None
    next_token = meta.get("next_token") if isinstance(meta, dict) else None
    if newest_id is None and mentions:
        newest_id = max((mention.post_id for mention in mentions), key=int)
    if newest_id is not None and (
        not isinstance(newest_id, str) or not newest_id.isdigit() or len(newest_id) > 19
    ):
        raise XPostOutcomeUnknown("X returned an invalid mentions cursor")
    if next_token is not None and (
        not isinstance(next_token, str) or not next_token or len(next_token) > 4096
    ):
        raise XPostOutcomeUnknown("X returned an invalid mentions pagination cursor")
    return XMentionsResult(
        mentions=tuple(mentions),
        fetched_count=len(raw_mentions),
        newest_id=newest_id,
        next_token=next_token,
    )


def post_is_available(access_token: str, post_id: str) -> bool:
    """Check that a previously captured reply target remains readable before dispatch."""
    if not post_id.isdigit() or len(post_id) > 19:
        return False
    try:
        with _client() as client:
            response = client.get(
                f"{POST_LOOKUP_URL}/{post_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"post.fields": "id"},
            )
    except httpx.HTTPError as exc:
        raise XPostOutcomeUnknown("X reply target could not be rechecked") from exc
    if response.status_code == 404:
        return False
    payload = _json_response(response, "reply target lookup")
    try:
        actual = payload["data"]["id"]  # type: ignore[index]
    except (KeyError, TypeError):
        raise XPostOutcomeUnknown("X reply target lookup response was invalid") from None
    return isinstance(actual, str) and actual == post_id
