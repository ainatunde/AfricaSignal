from __future__ import annotations

import logging

import httpx
import pytest

from africasignal.ops_heartbeat import ProcessHeartbeat, _endpoint


def test_heartbeat_adds_state_path_and_preserves_query() -> None:
    assert _endpoint("https://monitor.example/ping/token?source=worker", "start") == (
        "https://monitor.example/ping/token/start?source=worker"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://monitor.example/token",
        "https://user:password@monitor.example/token",
        "https://monitor.example/token#fragment",
        "not a URL",
    ],
)
def test_heartbeat_rejects_insecure_or_malformed_urls(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        ProcessHeartbeat("WORKER_HEARTBEAT_URL", url=url)


def test_heartbeat_ping_does_not_log_its_secret_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "https://monitor.example/private-token"

    def fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("network unavailable")

    monkeypatch.setattr("africasignal.ops_heartbeat.httpx.get", fail)
    caplog.set_level(logging.WARNING)
    assert ProcessHeartbeat("WORKER_HEARTBEAT_URL", url=secret).ping() is False
    assert secret not in caplog.text


def test_failed_process_cycle_suppresses_success_until_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def record(url: str, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(url)
        return httpx.Response(200)

    monkeypatch.setattr("africasignal.ops_heartbeat.httpx.get", record)
    monitor = ProcessHeartbeat("SCHEDULER_HEARTBEAT_URL", url="https://monitor.example/job")
    assert monitor.ping("fail") is True
    assert monitor.ping() is False
    assert monitor.mark_healthy() is True
    assert seen == ["https://monitor.example/job/fail", "https://monitor.example/job"]
