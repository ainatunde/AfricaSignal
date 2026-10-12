"""Sliding request limits shared in staging/production, local in development.

Shared state stores only an HMAC of the client address and expires after the window.
Raw client addresses never enter SQL, files or logs.
"""

from __future__ import annotations

import hashlib
import logging
import math
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable

from africasignal.shared_limits import SharedLimits, private_key, shared_enabled


class RateLimiter:
    def __init__(
        self,
        limit: int = 60,
        window_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
        scope: str = "public",
    ) -> None:
        self.scope = scope
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
        if shared_enabled():
            try:
                allowed, retry = SharedLimits().take(
                    self.scope,
                    private_key(self.scope, client),
                    rate=self.limit / self.window,
                    capacity=self.limit,
                    window=self.window,
                )
                return allowed, max(1, math.ceil(retry)) if not allowed else 0
            except Exception:
                logging.getLogger(__name__).exception(
                    "shared request limit unavailable", extra={"scope": self.scope}
                )
                return False, 1
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
