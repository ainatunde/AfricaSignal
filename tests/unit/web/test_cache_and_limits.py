from __future__ import annotations

from africasignal.web.cache import TTLCache
from africasignal.web.ratelimit import RateLimiter


def test_entries_expire_after_the_time_limit() -> None:
    now = [0.0]
    cache: TTLCache[str] = TTLCache(ttl_seconds=300, clock=lambda: now[0])
    cache.set(("s", 1), "page")
    assert cache.get(("s", 1)) == "page"
    assert cache.get(("s", 2)) is None  # another version is another key
    now[0] = 299
    assert cache.get(("s", 1)) == "page"
    now[0] = 301
    assert cache.get(("s", 1)) is None and len(cache) == 0


def test_the_cache_is_bounded_and_drops_the_least_recently_used() -> None:
    cache: TTLCache[int] = TTLCache(ttl_seconds=300, max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1  # a is now the most recently used
    cache.set("c", 3)
    assert cache.get("b") is None and cache.get("a") == 1 and cache.get("c") == 3


def test_the_limiter_allows_sixty_a_minute_then_refuses_with_a_wait() -> None:
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    assert all(limiter.check("c")[0] for _ in range(60))
    allowed, wait = limiter.check("c")
    assert not allowed and 1 <= wait <= 61
    now[0] = 60.5
    assert limiter.check("c")[0]
