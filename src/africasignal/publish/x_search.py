"""Bounded X Recent Search client that returns post IDs only."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"


class XSearchRejected(ValueError):
    """X definitively rejected a search request."""


class XSearchOutcomeUnknown(RuntimeError):
    """A search may have been billed despite the missing response."""


@dataclass(frozen=True)
class XSearchResult:
    post_ids: tuple[str, ...]
    fetched_count: int
    newest_id: str | None


def search_recent(
    bearer_token: str, query: str, max_results: int, since_id: str | None
) -> XSearchResult:
    """Search only X's recent seven-day index; return IDs and discard all post content in memory."""
    params = {
        "query": query,
        "max_results": str(max_results),
    }
    if since_id:
        params["since_id"] = since_id
    try:
        with httpx.Client(
            timeout=httpx.Timeout(15.0, connect=3.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(
                SEARCH_URL,
                headers={"Authorization": f"Bearer {bearer_token}"},
                params=params,
            )
    except httpx.HTTPError as exc:
        raise XSearchOutcomeUnknown(
            "X search outcome is unknown; its full result allowance remains reserved"
        ) from exc

    if 400 <= response.status_code < 500:
        raise XSearchRejected(f"X rejected the search (HTTP {response.status_code})")
    if response.status_code < 200 or response.status_code >= 300:
        raise XSearchOutcomeUnknown(
            f"X returned HTTP {response.status_code}; the search outcome is unknown"
        )
    try:
        payload = response.json()
        raw_posts = payload.get("data", [])
        if not isinstance(raw_posts, list) or len(raw_posts) > max_results:
            raise TypeError
        ids = tuple(
            post_id
            for item in raw_posts
            if isinstance(item, dict)
            and isinstance((post_id := item.get("id")), str)
            and post_id.isdigit()
        )
        if len(ids) != len(raw_posts):
            raise ValueError
        meta = payload.get("meta", {})
        newest_id = meta.get("newest_id") if isinstance(meta, dict) else None
        if newest_id is None and ids:
            newest_id = max(ids, key=int)
        if newest_id is not None and (not isinstance(newest_id, str) or not newest_id.isdigit()):
            raise ValueError
    except (ValueError, TypeError):
        raise XSearchOutcomeUnknown(
            "X returned an invalid search response; the result allowance remains reserved"
        ) from None
    unique_ids = tuple(dict.fromkeys(ids))
    return XSearchResult(post_ids=unique_ids, fetched_count=len(raw_posts), newest_id=newest_id)
