"""Exercise the real killable subprocess boundary, without external DNS."""

import time

from africasignal.net import resolver


def test_timeout_reaps_child_and_returns_fail_closed(monkeypatch):
    monkeypatch.setattr(resolver, "_RESOLVE_SCRIPT", "import time; time.sleep(30)")
    started = time.monotonic()
    with resolver.dns_budget(0.15):
        assert resolver.dns_addresses("example.org", 443) == []
    assert time.monotonic() - started < 2
    # A timed-out lookup releases its capacity and cannot poison later calls.
    monkeypatch.setattr(resolver, "_RESOLVE_SCRIPT", "print('[\"93.184.216.34\"]')")
    assert resolver.dns_addresses("example.org", 443) == ["93.184.216.34"]


def test_exhausted_budget_does_not_spawn(monkeypatch):
    monkeypatch.setattr(
        resolver.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned"))
    )
    with resolver.dns_budget(0):
        assert resolver.dns_addresses("example.org", 443) == []


def test_fetch_dns_budget_honors_positional_deadline(monkeypatch):
    from contextlib import contextmanager

    from africasignal.net import fetch

    budgets = []

    @contextmanager
    def record(seconds):
        budgets.append(seconds)
        yield

    monkeypatch.setattr(fetch, "dns_budget", record)
    result = fetch.fetch_document("", None, 15, 100, False, None, None, 0.25)
    assert result.error is not None
    assert budgets == [0.25]
