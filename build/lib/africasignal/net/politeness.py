"""Politeness, rate limiting, and robots.txt compliance for honest web acquisition.

Copied from TV Insights (``app/scraping/politeness.py``); the per-domain rate can now be set from
a source's ``max_requests_per_hour``.

Enforces:
- Zero evasion: robots.txt is checked before every request; a disallow means abstaining.
- Per-domain in-process token-bucket rate limiter (thread-safe, with capacity guards).
- Bypass for local/file schemes (file://) without network dependencies.
- Fail-closed handling for malformed or unparseable URLs.
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

from africasignal.net.netutil import USER_AGENT, allowed_by_robots

log = logging.getLogger("africasignal.net.politeness")


class TokenBucketRateLimiter:
    """Thread-safe per-domain token-bucket rate limiter."""

    def __init__(self, rate: float = 1.0, capacity: float = 1.0) -> None:
        """Initialize limiter with refill rate (tokens/second) and burst capacity."""
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._lock = threading.Lock()
        # domain -> (current_tokens, last_refill_timestamp)
        self._buckets: dict[str, tuple[float, float]] = {}
        # domain -> (rate, capacity) overriding the defaults, for example from a source's budget
        self._limits: dict[str, tuple[float, float]] = {}

    def configure_domain(self, domain: str, rate: float, capacity: float) -> None:
        """Give one domain its own refill rate (tokens/second) and burst capacity."""
        with self._lock:
            if self._limits.get(domain) != (rate, capacity):
                self._limits[domain] = (rate, capacity)
                self._buckets.pop(domain, None)

    def _limit_for(self, domain: str) -> tuple[float, float]:
        return self._limits.get(domain, (self.rate, self.capacity))

    def acquire(self, domain: str, tokens: float = 1.0) -> bool:
        """Attempt to acquire tokens for domain. Returns True if granted, False if exhausted."""
        tokens = float(tokens)
        now = time.monotonic()
        with self._lock:
            rate, capacity = self._limit_for(domain)
            if tokens > capacity:
                return False
            current_tokens, last_time = self._buckets.get(domain, (capacity, now))
            elapsed = now - last_time
            current_tokens = min(capacity, current_tokens + elapsed * rate)

            if current_tokens >= tokens:
                current_tokens -= tokens
                self._buckets[domain] = (current_tokens, now)
                return True

            self._buckets[domain] = (current_tokens, now)
            return False

    def wait_and_acquire(self, domain: str, tokens: float = 1.0, max_wait: float = 2.0) -> bool:
        """Wait up to max_wait seconds to acquire tokens. True if acquired, False otherwise."""
        tokens = float(tokens)
        deadline = time.monotonic() + max_wait
        while True:
            now = time.monotonic()
            with self._lock:
                rate, capacity = self._limit_for(domain)
                if tokens > capacity:
                    return False
                current_tokens, last_time = self._buckets.get(domain, (capacity, now))
                elapsed = now - last_time
                current_tokens = min(capacity, current_tokens + elapsed * rate)

                if current_tokens >= tokens:
                    current_tokens -= tokens
                    self._buckets[domain] = (current_tokens, now)
                    return True

                needed = tokens - current_tokens
                wait_time = needed / rate
                self._buckets[domain] = (current_tokens, now)

            if now + wait_time > deadline:
                return False

            sleep_duration = min(wait_time, max(0.001, deadline - now))
            time.sleep(sleep_duration)

    def reset(self) -> None:
        """Clear all bucket state and restore fresh capacity across all domains."""
        with self._lock:
            self._buckets.clear()
            self._limits.clear()


# Burst size used when a domain is limited by an hourly budget.
MAX_BURST = 5

# Default shared singleton rate limiter (1 req/sec per domain)
rate_limiter = TokenBucketRateLimiter(rate=1.0, capacity=1.0)


def politeness_gate(
    url: str,
    user_agent: str | None = None,
    acquire_token: bool = True,
    max_requests_per_hour: int | None = None,
) -> bool:
    """Evaluate whether url may be acquired per robots.txt and domain rate limits.

    Returns:
        True: URL is permitted and rate limit token was acquired (or dry-run requested).
        False: URL is disallowed, rate limited, malformed, or invalid (caller must abstain).
    """
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    # Local file schemes bypass robots and rate limiting
    if parsed.scheme == "file":
        return True

    # Must be standard http/https with non-empty network location
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    # 1. Zero-evasion robots.txt check
    ua = user_agent or USER_AGENT
    if not allowed_by_robots(url, user_agent=ua):
        log.warning("Politeness gate: URL %s disallowed by robots.txt; abstaining from fetch.", url)
        return False

    # 2. Rate limit check
    if not acquire_token:
        return True

    domain = (parsed.hostname or parsed.netloc).lower()
    if max_requests_per_hour is not None and max_requests_per_hour > 0:
        # Steady rate from the source's hourly budget, with a small burst so that a listing page
        # and a few articles can be fetched back to back.
        rate_limiter.configure_domain(
            domain,
            rate=max_requests_per_hour / 3600.0,
            capacity=float(min(max_requests_per_hour, MAX_BURST)),
        )
    if not rate_limiter.acquire(domain, tokens=1.0):
        log.warning(
            "Politeness gate: Rate limit exceeded for domain %s; throttling request.", domain
        )
        return False

    return True
