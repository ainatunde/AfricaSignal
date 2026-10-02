import concurrent.futures
import time

import pytest

from africasignal.net import politeness
from africasignal.net.politeness import TokenBucketRateLimiter, politeness_gate


def test_capacity_is_exhausted() -> None:
    limiter = TokenBucketRateLimiter(rate=2.0, capacity=2.0)
    assert limiter.acquire("a.ng") is True
    assert limiter.acquire("a.ng") is True
    assert limiter.acquire("a.ng") is False


def test_domains_do_not_starve_each_other() -> None:
    limiter = TokenBucketRateLimiter(rate=1.0, capacity=1.0)
    assert limiter.acquire("one.com") is True
    assert limiter.acquire("one.com") is False
    assert limiter.acquire("two.org") is True


def test_request_larger_than_capacity_is_rejected() -> None:
    assert TokenBucketRateLimiter(rate=2.0, capacity=2.0).acquire("x.ng", tokens=3.0) is False


def test_tokens_replenish_over_time() -> None:
    limiter = TokenBucketRateLimiter(rate=10.0, capacity=1.0)
    assert limiter.acquire("r.ng") is True
    assert limiter.acquire("r.ng") is False
    time.sleep(0.12)
    assert limiter.acquire("r.ng") is True


def test_wait_and_acquire_waits_within_max_wait_only() -> None:
    limiter = TokenBucketRateLimiter(rate=5.0, capacity=1.0)
    assert limiter.wait_and_acquire("w.ng", max_wait=0.5) is True
    t0 = time.monotonic()
    assert limiter.wait_and_acquire("w.ng", max_wait=0.5) is True
    assert time.monotonic() - t0 >= 0.15
    t1 = time.monotonic()
    assert limiter.wait_and_acquire("w.ng", max_wait=0.01) is False
    assert time.monotonic() - t1 < 0.1


def test_concurrent_threads_cannot_over_consume() -> None:
    limiter = TokenBucketRateLimiter(rate=0.01, capacity=3.0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: limiter.acquire("c.ng"), range(10)))
    assert results.count(True) == 3


def test_reset_restores_capacity_and_clears_domain_limits() -> None:
    limiter = TokenBucketRateLimiter(rate=1.0, capacity=1.0)
    limiter.configure_domain("r.ng", rate=0.001, capacity=1.0)
    assert limiter.acquire("r.ng") and not limiter.acquire("r.ng")
    limiter.reset()
    assert limiter.acquire("r.ng")


def test_domain_limit_overrides_defaults() -> None:
    limiter = TokenBucketRateLimiter(rate=1000.0, capacity=1000.0)
    limiter.configure_domain("slow.ng", rate=0.001, capacity=2.0)
    assert [limiter.acquire("slow.ng") for _ in range(3)] == [True, True, False]
    assert limiter.acquire("fast.ng") is True  # other domains keep the defaults


def test_reconfiguring_with_the_same_values_keeps_the_bucket() -> None:
    limiter = TokenBucketRateLimiter()
    limiter.configure_domain("s.ng", rate=0.001, capacity=1.0)
    assert limiter.acquire("s.ng") is True
    limiter.configure_domain("s.ng", rate=0.001, capacity=1.0)
    assert limiter.acquire("s.ng") is False


def test_gate_abstains_when_robots_disallows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(politeness, "allowed_by_robots", lambda url, user_agent=None: False)
    url = "https://forbidden.example.ng/schedule"
    assert politeness_gate(url, acquire_token=True) is False
    assert politeness_gate(url, acquire_token=False) is False


def test_gate_rate_limits_a_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(politeness, "allowed_by_robots", lambda url, user_agent=None: True)
    monkeypatch.setattr(politeness, "rate_limiter", TokenBucketRateLimiter(rate=1.0, capacity=1.0))
    url = "https://limited.example.ng/grid"
    assert politeness_gate(url, acquire_token=False) is True  # dry run consumes nothing
    results = [politeness_gate(url) for _ in range(20)]
    assert results[0] is True and False in results


def test_gate_uses_the_sources_hourly_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(politeness, "allowed_by_robots", lambda url, user_agent=None: True)
    monkeypatch.setattr(
        politeness, "rate_limiter", TokenBucketRateLimiter(rate=1000, capacity=1000)
    )
    url = "https://budgeted.example.ng/a"
    # 60 per hour is one per minute with a burst of five: the sixth immediate request is refused.
    results = [politeness_gate(url, max_requests_per_hour=60) for _ in range(7)]
    assert results == [True] * 5 + [False] * 2


def test_local_file_urls_bypass_the_gate() -> None:
    assert politeness_gate("file:///tmp/sample.xml") is True


@pytest.mark.parametrize(
    "bad", ["", "javascript:alert(1)", "not-a-url", "ftp://x.example/f", "http://"]
)
def test_gate_fails_closed_on_invalid_urls(bad: str) -> None:
    assert politeness_gate(bad) is False


def test_crawl_delay_forces_single_request_spacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(politeness, "allowed_by_robots", lambda url, user_agent=None: True)
    monkeypatch.setattr(
        politeness.netutil, "robots_pacing", lambda url, user_agent=None: (7.0, None)
    )
    monkeypatch.setattr(
        politeness, "rate_limiter", TokenBucketRateLimiter(rate=1000.0, capacity=1000.0)
    )
    url = "https://slow.example.ng/a"
    assert politeness_gate(url) is True
    assert politeness_gate(url) is False


def test_robots_request_rate_bounds_burst_and_steady_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(politeness, "allowed_by_robots", lambda url, user_agent=None: True)
    monkeypatch.setattr(
        politeness.netutil, "robots_pacing", lambda url, user_agent=None: (None, (3, 60))
    )
    monkeypatch.setattr(
        politeness, "rate_limiter", TokenBucketRateLimiter(rate=1000.0, capacity=1000.0)
    )
    results = [politeness_gate("https://limited.example.ng/a") for _ in range(5)]
    assert results == [True, True, True, False, False]
