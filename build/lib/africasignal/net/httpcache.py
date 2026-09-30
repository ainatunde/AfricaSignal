"""Copied from TV Insights (``app/scraping/httpcache.py``).

RFC 7232 / RFC 9110 conditional GET HTTP caching layer for polite scraping.

Provides:
- In-memory thread-safe conditional caching storing ETag and Last-Modified validators.
- Case-insensitive header normalization (supports HTTP/1.1 and lowercase HTTP/2 headers).
- Extraction of If-None-Match and If-Modified-Since headers for outbound requests.
- Strict refusal to cache non-200 responses as fresh entries.
- Revalidation payload recovery on HTTP 304 Not Modified.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class CachedResponse:
    """Cached HTTP response payload with normalized validation headers."""

    url: str
    status_code: int
    headers: dict[str, str]
    content: str | bytes
    cached_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def etag(self) -> str | None:
        return self._get_ci_header("etag")

    @property
    def last_modified(self) -> str | None:
        return self._get_ci_header("last-modified")

    def _get_ci_header(self, target: str) -> str | None:
        target_lower = target.lower()
        for k, v in self.headers.items():
            if k.lower() == target_lower:
                return v
        return None

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        return default

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)


class HttpCache:
    """Thread-safe conditional HTTP cache."""

    def __init__(self) -> None:
        self._cache: dict[str, CachedResponse] = {}
        self._lock = threading.Lock()

    def get_conditional_headers(self, url: str) -> dict[str, str]:
        """Generate conditional headers (If-None-Match, If-Modified-Since) for outbound request."""
        with self._lock:
            cached = self._cache.get(url)

        if cached is None:
            return {}

        headers: dict[str, str] = {}
        etag = cached.etag
        if etag:
            headers["If-None-Match"] = etag

        last_modified = cached.last_modified
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        return headers

    def is_cached(self, url: str) -> bool:
        """Return True if url has a valid cached 200 response."""
        with self._lock:
            return url in self._cache

    def get_cached_response(self, url: str) -> CachedResponse | None:
        """Retrieve cached response object or None if not found."""
        with self._lock:
            return self._cache.get(url)

    def store_response(
        self,
        url: str,
        status_code: int,
        headers: dict[str, str] | None,
        content: str | bytes,
    ) -> None:
        """Store HTTP response if status_code is 200 OK. Ignores error/revalidation codes."""
        if status_code != 200:
            return

        normalized_headers = dict(headers or {})
        entry = CachedResponse(
            url=url,
            status_code=status_code,
            headers=normalized_headers,
            content=content,
        )
        with self._lock:
            self._cache[url] = entry

    def invalidate(self, url: str) -> None:
        """Remove a cached URL entry."""
        with self._lock:
            self._cache.pop(url, None)

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()
