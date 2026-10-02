"""Fixed metadata-only adapter for the official YouTube Data API search endpoint."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from agent_reach_runner.schemas import RunnerCandidate, TaskSubmission

API_HOST = "www.googleapis.com"
API_PATH = "/youtube/v3/search"
BACKEND_ID = "youtube_data_api_v3"
MAX_API_RESPONSE_BYTES = 256_000
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class BackendFailure(RuntimeError):
    """A sanitized search-backend failure that is safe to store in task state."""


class YouTubeSearchBackend:
    def __init__(self, *, http_client_factory: Callable[..., httpx.Client] = httpx.Client) -> None:
        self.http_client_factory = http_client_factory

    @staticmethod
    def _api_key() -> str:
        path = os.environ.get("AGENT_REACH_YOUTUBE_API_KEY_FILE", "")
        if not path:
            raise BackendFailure("The configured search capability is unavailable.")
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            raise BackendFailure("The configured search capability is unavailable.") from None
        if not value or len(value) > 256 or any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
            raise BackendFailure("The configured search capability is unavailable.")
        return value

    @classmethod
    def configured(cls) -> bool:
        return (
            os.environ.get("AGENT_REACH_YOUTUBE_ENABLED", "false").lower() == "true"
            and bool(os.environ.get("AGENT_REACH_YOUTUBE_API_KEY_FILE"))
            and Path(os.environ["AGENT_REACH_YOUTUBE_API_KEY_FILE"]).is_file()
        )

    @staticmethod
    def daily_search_limit() -> int:
        raw = os.environ.get("AGENT_REACH_YOUTUBE_SEARCHES_PER_QUOTA_DAY", "10")
        try:
            value = int(raw)
        except ValueError:
            raise RuntimeError(
                "AGENT_REACH_YOUTUBE_SEARCHES_PER_QUOTA_DAY must be an integer"
            ) from None
        if not 1 <= value <= 100:
            raise RuntimeError(
                "AGENT_REACH_YOUTUBE_SEARCHES_PER_QUOTA_DAY must be between 1 and 100"
            )
        return value

    def search(self, request: TaskSubmission) -> list[RunnerCandidate]:
        key = self._api_key()
        proxy = os.environ.get("AGENT_REACH_HTTPS_PROXY", "http://egress-proxy:3128")
        if not proxy.startswith("http://egress-proxy:"):
            raise BackendFailure("The isolated egress proxy is unavailable.")
        query = f"Nigeria {request.topic} {request.query}"[:500]
        params = {
            "key": key,
            "part": "snippet",
            "type": "video",
            "regionCode": request.country,
            "relevanceLanguage": "en",
            "safeSearch": "strict",
            "maxResults": str(request.max_results),
            "q": query,
            "fields": "items(id/videoId,snippet(title,channelTitle,channelId,publishedAt))",
        }
        try:
            with self.http_client_factory(
                proxy=proxy,
                timeout=httpx.Timeout(connect=5, read=12, write=5, pool=5),
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "AfricaSignal-AgentReach/1.0",
                },
            ) as client:
                with client.stream(
                    "GET", f"https://{API_HOST}{API_PATH}", params=params
                ) as response:
                    if response.status_code != 200:
                        if response.status_code in (403, 429):
                            raise BackendFailure(
                                "The search provider rejected the request or its quota "
                                "is exhausted."
                            )
                        raise BackendFailure(
                            "The search provider returned an unsuccessful response."
                        )
                    declared = response.headers.get("content-length")
                    if declared and int(declared) > MAX_API_RESPONSE_BYTES:
                        raise BackendFailure(
                            "The search provider response exceeded its size limit."
                        )
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_API_RESPONSE_BYTES:
                            raise BackendFailure(
                                "The search provider response exceeded its size limit."
                            )
        except BackendFailure:
            raise
        except (httpx.HTTPError, ValueError, OSError):
            raise BackendFailure("The search provider is temporarily unavailable.") from None

        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            raise BackendFailure("The search provider returned invalid data.") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
            raise BackendFailure("The search provider returned invalid data.")

        retrieved_at = datetime.now(UTC)
        results: list[RunnerCandidate] = []
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            video_id = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
                continue
            title = snippet.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            publisher = snippet.get("channelTitle")
            published = snippet.get("publishedAt")
            published_at = None
            if isinstance(published, str):
                try:
                    parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    published_at = parsed if parsed.tzinfo is not None else None
                except ValueError:
                    published_at = None
            results.append(
                RunnerCandidate(
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    title=title[:300],
                    summary=None,
                    publisher=publisher[:200] if isinstance(publisher, str) else None,
                    platform="youtube",
                    backend=BACKEND_ID,
                    published_at=published_at,
                    retrieved_at=retrieved_at,
                )
            )
        return results[: request.max_results]
