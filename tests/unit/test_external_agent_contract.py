from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from africasignal.external_agents.client import (
    AgentTaskReply,
    ExternalAgentClient,
    ExternalAgentProtocolError,
)
from africasignal.operations.external_agents import ProfileDraft, TaskRequest


def draft(**overrides: object) -> ProfileDraft:
    value: dict[str, object] = {
        "slug": "review-agent",
        "display_name": "Review agent",
        "endpoint_url": "https://agent.example",
        "credential": "bearer-test-credential",
        "allowed_purposes": ["research", "summarize"],
        "allowed_domains": ["example.org"],
        "max_steps": 4,
        "timeout_seconds": 90,
        "max_output_bytes": 4096,
        "max_tasks_per_day": 5,
        "max_concurrency": 1,
        "max_cost_per_task_usd": Decimal("0.10"),
        "max_spend_per_day_usd": Decimal("0.50"),
    }
    value.update(overrides)
    return ProfileDraft.model_validate(value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://agent.example",
        "https://user:secret@agent.example",
        "https://agent.example/v1",
        "https://agent.example?debug=1",
        "https://localhost",
        "https://127.0.0.1",
        "https://[::1]",
        "https://host.internal",
    ],
)
def test_profile_rejects_non_public_or_non_origin_endpoints(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        draft(endpoint_url=endpoint)


@pytest.mark.parametrize("domain", ["*.example.org", "localhost", "10.0.0.1", "example.local"])
def test_profile_rejects_wildcard_and_private_domain_scopes(domain: str) -> None:
    with pytest.raises(ValidationError):
        draft(allowed_domains=[domain])


def test_profile_caps_purposes_and_cost_reservations() -> None:
    with pytest.raises(ValidationError):
        draft(allowed_purposes=["research", "research"])
    with pytest.raises(ValidationError):
        draft(max_spend_per_day_usd=Decimal("0.05"))


def test_task_request_rejects_urls_contact_details_and_control_characters() -> None:
    with pytest.raises(ValidationError):
        TaskRequest(purpose="research", objective="Read https://private.example/document")
    with pytest.raises(ValidationError):
        TaskRequest(purpose="research", objective="Contact user@example.org")
    with pytest.raises(ValidationError):
        TaskRequest(purpose="research", objective="Research\x00 this public topic")
    request = TaskRequest(purpose="research", objective="  Public power outage trends  ")
    assert request.objective == "Public power outage trends"


def _profile(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "slug": "review-agent",
        "endpoint_url": "https://agent.example",
        "allowed_purposes": ["research"],
        "max_concurrency": 1,
        "max_steps": 4,
        "timeout_seconds": 90,
        "max_output_bytes": 64,
        "max_tasks_per_day": 5,
        "max_cost_per_task_usd": Decimal("0.10"),
        "max_spend_per_day_usd": Decimal("0.50"),
        "allowed_domains": ["example.org"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_submission_sends_only_scoped_public_metadata_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ExternalAgentClient(_profile(), "bearer-test-credential")
    task = SimpleNamespace(
        id=7,
        idempotency_key="africasignal:external-agent:review-agent:unique",
        purpose="research",
        objective="Public power outage trends",
        deadline_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    seen: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> tuple[int, bytes]:
        seen.update({"method": method, "path": path, **kwargs})
        reply = {
            "protocol_version": "africasignal.external-agent/1",
            "task_id": "7",
            "external_task_id": "remote-1",
            "status": "queued",
        }
        return 202, json.dumps(reply).encode()

    monkeypatch.setattr(client, "_request", fake_request)
    result = client.submit(task, _profile())

    assert result.status == "queued"
    assert seen["method"] == "POST" and seen["path"] == "/v1/tasks"
    assert seen["extra_headers"] == {"Idempotency-Key": task.idempotency_key}
    body = seen["json_body"]
    assert isinstance(body, dict)
    assert body["data_classification"] == "public_metadata"
    assert body["allowed_domains"] == ["example.org"]
    assert body["objective"] == task.objective
    assert "credential" not in body


def test_protocol_rejects_wrong_task_identity_and_output_byte_overflow() -> None:
    client = ExternalAgentClient(_profile(max_output_bytes=4), "bearer-test-credential")
    task = SimpleNamespace(id=7)
    wrong = {
        "protocol_version": "africasignal.external-agent/1",
        "task_id": "8",
        "external_task_id": "remote-1",
        "status": "succeeded",
        "result_text": "safe",
    }
    with pytest.raises(ExternalAgentProtocolError, match="different task identity"):
        client._parse_reply(json.dumps(wrong).encode(), task, _profile(max_output_bytes=4))

    wrong["task_id"] = "7"
    wrong["result_text"] = "€€"
    with pytest.raises(ExternalAgentProtocolError, match="byte limit"):
        client._parse_reply(json.dumps(wrong).encode(), task, _profile(max_output_bytes=4))


def test_success_after_cancel_is_recorded_as_untrusted_late_completion() -> None:
    from africasignal.jobs.handlers import external_agents as handler

    task = SimpleNamespace(
        id=7,
        status="cancellation_requested",
        external_task_id=None,
        reported_usage=None,
        started_at=None,
        finished_at=None,
        result_text=None,
        last_error=None,
    )
    reply = AgentTaskReply(
        protocol_version="africasignal.external-agent/1",
        task_id="7",
        external_task_id="remote-1",
        status="succeeded",
        result_text="untrusted result",
    )
    handler._apply_reply(object(), _profile(), task, reply, datetime.now(UTC))
    assert task.status == "succeeded"
    assert task.result_text == "untrusted result"
    assert "after cancellation" in task.last_error
