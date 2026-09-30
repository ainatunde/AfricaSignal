"""In-memory rate limit for the public API: 60 requests per minute per client (B11.4).

The client address is only ever a key in this process's memory, salted and hashed, and entries
disappear when their window ends. Nothing is written to the database, a file or a log.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        limit: int = 60,
        window_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._salt = secrets.token_bytes(16)  # new on every start, so keys mean nothing outside
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._next_sweep = 0.0

    def _key(self, client: str) -> str:
        return hashlib.sha256(self._salt + client.encode()).hexdigest()[:24]

    def check(self, client: str) -> tuple[bool, int]:
        """Record a request. Returns (allowed, seconds to wait when refused)."""
        now = self._clock()
        key = self._key(client)
        with self._lock:
            if now >= self._next_sweep:
                self._sweep(now)
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, max(1, int(hits[0] + self.window - now) + 1)
            hits.append(now)
            return True, 0

    def _sweep(self, now: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or h[-1] <= now - self.window]:
            del self._hits[key]
        self._next_sweep = now + self.window

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
