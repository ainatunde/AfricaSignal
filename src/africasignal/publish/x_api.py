"""Minimal X v2 user-context client for one-time text publication."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

POSTS_URL = "https://api.x.com/2/tweets"


class XPostRejected(ValueError):
    """X returned a response that definitively rejected the post."""


class XPostOutcomeUnknown(RuntimeError):
    """The request may have reached X; callers must not retry it automatically."""


@dataclass(frozen=True)
class XPostReceipt:
    post_id: str


def create_post(access_token: str, text: str) -> XPostReceipt:
    """Create one text post using an OAuth 2.0 user-context access token.

    The client disables redirects and ambient proxy configuration. A transport error, 5xx, or
    malformed success response is ambiguous and must be reconciled by an operator, never retried.
    """
    try:
        with httpx.Client(
            timeout=httpx.Timeout(10.0, connect=3.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.post(
                POSTS_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                json={"text": text},
            )
    except httpx.HTTPError as exc:
        raise XPostOutcomeUnknown(
            "X request outcome is unknown; automatic retry is disabled"
        ) from exc

    if 400 <= response.status_code < 500:
        raise XPostRejected(f"X rejected the post (HTTP {response.status_code})")
    if response.status_code < 200 or response.status_code >= 300:
        raise XPostOutcomeUnknown(
            f"X returned HTTP {response.status_code}; the request outcome is unknown"
        )
    try:
        post_id = response.json()["data"]["id"]
    except (ValueError, KeyError, TypeError):
        raise XPostOutcomeUnknown(
            "X accepted a response without a usable post id; automatic retry is disabled"
        ) from None
    if not isinstance(post_id, str) or not post_id.isdigit():
        raise XPostOutcomeUnknown("X returned an invalid post id; automatic retry is disabled")
    return XPostReceipt(post_id=post_id)
