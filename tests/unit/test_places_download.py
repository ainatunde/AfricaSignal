"""Boundary downloads wait out the per-domain rate limit instead of failing."""

import pytest

from africasignal.net.fetch import FetchResult
from africasignal.places import load


def test_fetch_politely_retries_when_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = [
        FetchResult(url="u", abstained=True, error="rate limit"),
        FetchResult(url="u", status_code=200, content=b"ok"),
    ]
    sleeps: list[float] = []
    monkeypatch.setattr(load, "fetch_document", lambda *a, **k: calls.pop(0))
    monkeypatch.setattr(load.time, "sleep", sleeps.append)

    result = load._fetch_politely("u")

    assert result.success and result.content == b"ok"
    assert sleeps == [1.2]


def test_fetch_politely_gives_up_after_three_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    n = 0

    def always_abstain(*a: object, **k: object) -> FetchResult:
        nonlocal n
        n += 1
        return FetchResult(url="u", abstained=True, error="robots")

    monkeypatch.setattr(load, "fetch_document", always_abstain)
    monkeypatch.setattr(load.time, "sleep", lambda s: None)

    assert not load._fetch_politely("u").success
    assert n == 4
