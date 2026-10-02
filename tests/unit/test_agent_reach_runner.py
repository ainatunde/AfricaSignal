from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runner"))
from agent_reach_runner.backend import BackendFailure, YouTubeSearchBackend
from agent_reach_runner.egress_proxy import ALLOWED_HOST, _parse_connect
from agent_reach_runner.schemas import RunnerCandidate, TaskSubmission
from agent_reach_runner.store import TaskConflict, TaskStore, youtube_quota_day


def submission(
    task_id: str = "123",
    *,
    deadline_at: datetime | None = None,
    query: str = "power outage reporting",
) -> TaskSubmission:
    return TaskSubmission(
        task_id=task_id,
        idempotency_key=f"africasignal:agent-reach:{task_id}",
        control_generation=7,
        config_revision=7,
        capability="public_search_metadata",
        topic="energy",
        query=query,
        country="NG",
        deadline_at=deadline_at or datetime.now(UTC) + timedelta(minutes=20),
        max_results=5,
        max_output_bytes=128_000,
        policy={
            "fetch_candidate_pages": False,
            "fetch_transcripts": False,
            "download_attachments": False,
            "publish_or_notify": False,
        },
    )


def test_submission_requires_timezone_and_false_policy() -> None:
    request = submission().model_dump(mode="python")
    request["deadline_at"] = datetime.now()
    with pytest.raises(ValidationError):
        TaskSubmission.model_validate(request)

    request = submission().model_dump(mode="python")
    request["policy"]["fetch_transcripts"] = True
    with pytest.raises(ValidationError):
        TaskSubmission.model_validate(request)


def test_expired_task_is_terminal_and_not_dispatched(tmp_path) -> None:
    store = TaskStore(tmp_path / "runner.sqlite3")
    accepted = store.enqueue(submission(deadline_at=datetime.now(UTC) - timedelta(minutes=1)))

    assert store.claim_next() is None
    expired = store.get(accepted.external_task_id)
    assert expired is not None
    assert expired.status == "expired"
    assert expired.task_id == "123"
    assert expired.control_generation == 7
    assert expired.results == []


def test_idempotency_conflicts_on_changed_payload_and_closes_connections(tmp_path) -> None:
    store = TaskStore(tmp_path / "runner.sqlite3")
    request = submission()
    first = store.enqueue(request)
    retry = store.enqueue(request)
    assert store.get_by_task_id("123").external_task_id == first.external_task_id

    assert retry.external_task_id == first.external_task_id
    with pytest.raises(TaskConflict):
        store.enqueue(request.model_copy(update={"query": "different energy query"}))

    connection = store._connect()
    with connection:
        connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_runner_restart_does_not_retry_uncertain_provider_call(tmp_path) -> None:
    store = TaskStore(tmp_path / "runner.sqlite3")
    accepted = store.enqueue(submission())
    claimed = store.claim_next()
    assert claimed is not None
    assert claimed[0] == accepted.external_task_id

    store.recover_interrupted()

    recovered = store.get(accepted.external_task_id)
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.error == (
        "Provider outcome is unknown after runner restart; no automatic retry was issued."
    )
    assert store.claim_next() is None


def test_retention_purges_api_results_and_search_query_but_keeps_idempotency(tmp_path) -> None:
    store = TaskStore(tmp_path / "runner.sqlite3")
    request = submission()
    accepted = store.enqueue(request)
    claimed = store.claim_next()
    assert claimed is not None
    external_id, _ = claimed
    candidate = RunnerCandidate(
        url="https://www.youtube.com/watch?v=abcdefghijk",
        title="Sensitive search result title",
        summary=None,
        publisher="Channel",
        platform="youtube",
        backend="youtube_data_api_v3",
        published_at=datetime.now(UTC) - timedelta(days=1),
        retrieved_at=datetime.now(UTC),
    )
    store.complete(external_id, [candidate])
    assert store.get(external_id).status == "succeeded"

    old = (datetime.now(UTC) - timedelta(days=29)).isoformat()
    with store._connect() as connection:
        connection.execute(
            "UPDATE runner_task SET created_at=? WHERE external_task_id=?",
            (old, external_id),
        )

    assert store.purge_expired_results() == 1
    expired = store.get(external_id)
    assert expired is not None
    assert expired.status == "expired"
    assert expired.results == []
    assert expired.error == "Task data expired under the retention policy."
    with store._connect() as connection:
        row = connection.execute(
            "SELECT payload_json,results_json FROM runner_task WHERE external_task_id=?",
            (external_id,),
        ).fetchone()
    assert "[expired query]" in row["payload_json"]
    assert request.query not in row["payload_json"]
    assert "Sensitive search result title" not in row["results_json"]

    # The original idempotency hash remains a tombstone, so a delayed retry cannot spend again.
    assert store.enqueue(request).external_task_id == accepted.external_task_id
    assert store.purge_expired_results() == 0


def test_quota_day_uses_youtube_pacific_reset() -> None:
    before_midnight = datetime(2026, 10, 2, 6, 59, tzinfo=UTC)
    after_midnight = datetime(2026, 10, 2, 7, 1, tzinfo=UTC)
    assert youtube_quota_day(before_midnight) == "2026-10-01"
    assert youtube_quota_day(after_midnight) == "2026-10-02"


def test_search_reservation_cap_is_atomic_and_bounded(tmp_path) -> None:
    store = TaskStore(tmp_path / "runner.sqlite3")
    assert store.reserve_search(2)
    assert store.reserve_search(2)
    assert not store.reserve_search(2)


def test_proxy_accepts_only_the_fixed_google_connect_target() -> None:
    good = f"CONNECT {ALLOWED_HOST}:443 HTTP/1.1\r\nHost: {ALLOWED_HOST}:443\r\n\r\n".encode()
    wrong_host = b"CONNECT attacker.example:443 HTTP/1.1\r\nHost: attacker.example:443\r\n\r\n"
    mismatched_host_header = (
        f"CONNECT {ALLOWED_HOST}:443 HTTP/1.1\r\nHost: attacker.example:443\r\n\r\n".encode()
    )

    assert _parse_connect(good)
    assert not _parse_connect(wrong_host)
    assert not _parse_connect(mismatched_host_header)
    assert not _parse_connect(b"CONNECT www.googleapis.com:80 HTTP/1.1\r\n\r\n")


def test_youtube_adapter_uses_bounded_official_metadata_response(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "youtube-key"
    key_file.write_text("test-key", encoding="utf-8")
    monkeypatch.setenv("AGENT_REACH_YOUTUBE_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("AGENT_REACH_YOUTUBE_ENABLED", "true")

    seen: dict[str, object] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": {"videoId": "abcdefghijk"},
                        "snippet": {
                            "title": "Grid update",
                            "channelTitle": "Public Channel",
                            "publishedAt": "2026-09-20T10:00:00Z",
                        },
                    },
                    {"id": {"videoId": "invalid"}, "snippet": {"title": "Ignored"}},
                ]
            },
        )

    def client_factory(**kwargs):
        seen["proxy"] = kwargs["proxy"]
        seen["follow_redirects"] = kwargs["follow_redirects"]
        seen["trust_env"] = kwargs["trust_env"]
        seen["timeout"] = kwargs["timeout"]
        return httpx.Client(
            transport=httpx.MockTransport(responder),
            timeout=kwargs["timeout"],
            follow_redirects=kwargs["follow_redirects"],
            trust_env=kwargs["trust_env"],
            headers=kwargs["headers"],
        )

    results = YouTubeSearchBackend(http_client_factory=client_factory).search(submission())

    assert len(results) == 1
    assert results[0].url == "https://www.youtube.com/watch?v=abcdefghijk"
    assert results[0].summary is None
    assert results[0].backend == "youtube_data_api_v3"
    assert "www.googleapis.com/youtube/v3/search" in seen["url"]
    assert "safeSearch=strict" in seen["url"]
    assert "fields=" in seen["url"]
    assert seen["proxy"] == "http://egress-proxy:3128"
    assert seen["follow_redirects"] is False
    assert seen["trust_env"] is False
    assert isinstance(seen["timeout"], httpx.Timeout)


def test_youtube_adapter_does_not_follow_redirects(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "youtube-key"
    key_file.write_text("test-key", encoding="utf-8")
    monkeypatch.setenv("AGENT_REACH_YOUTUBE_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("AGENT_REACH_YOUTUBE_ENABLED", "true")
    requests: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://attacker.example/"})

    def client_factory(**kwargs):
        return httpx.Client(
            transport=httpx.MockTransport(responder),
            timeout=kwargs["timeout"],
            follow_redirects=kwargs["follow_redirects"],
            trust_env=kwargs["trust_env"],
            headers=kwargs["headers"],
        )

    with pytest.raises(BackendFailure):
        YouTubeSearchBackend(http_client_factory=client_factory).search(submission())
    assert len(requests) == 1
