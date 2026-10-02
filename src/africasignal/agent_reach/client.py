"""Bounded client for the AfricaSignal Agent Reach runner bridge.

Agent Reach itself is a capability installer, not this application's task HTTP API. The bridge is a
separately isolated service implementing docs/agent-reach-runner-contract.md.
"""

from __future__ import annotations

import ipaddress
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import AgentReachTask, Setting
from africasignal.net.netutil import is_safe_public_url, pinned_http_transport
from africasignal.settings_store import get as get_setting

MAX_RESPONSE_BYTES = 128_000
MAX_RESULTS = 20


class RunnerError(RuntimeError):
    """A sanitized connection or protocol error from the external runner."""


class RunnerCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=8, max_length=2048)
    title: str = Field(min_length=1, max_length=300)
    summary: str | None = Field(default=None, max_length=1000)
    publisher: str | None = Field(default=None, max_length=200)
    platform: str = Field(min_length=1, max_length=40)
    backend: str = Field(min_length=1, max_length=80)
    published_at: datetime | None = None
    retrieved_at: datetime

    @field_validator("url")
    @classmethod
    def public_http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or len(value) > 2048
        ):
            raise ValueError("candidate URL must be an absolute HTTP(S) URL without credentials")
        try:
            ip = ipaddress.ip_address(parts.hostname)
        except ValueError:
            ip = None
        if ip is not None and not ip.is_global:
            raise ValueError("candidate URL must use a public host")
        if not is_safe_public_url(value):
            raise ValueError("candidate URL did not resolve to a public host")
        return value

    @field_validator("published_at", "retrieved_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("runner timestamps must include a timezone")
        return value


class RunnerHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    protocol_version: Literal["africasignal-agent-reach/1"]
    capabilities: list[Literal["public_search_metadata"]] = Field(default_factory=list)
    backend: Literal["youtube_data_api_v3"]
    max_concurrency: int = Field(ge=1, le=4)


class RunnerTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    external_task_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=20, pattern=r"^[0-9]+$")
    control_generation: int = Field(ge=1)
    config_revision: int = Field(ge=1)
    status: Literal["queued", "running", "succeeded", "failed", "cancelled", "expired"]
    results: list[RunnerCandidate] = Field(default_factory=list, max_length=MAX_RESULTS)
    error: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def results_only_when_done(self) -> RunnerTaskResponse:
        if self.status != "succeeded" and self.results:
            raise ValueError("runner may return candidates only for a succeeded task")
        return self


class AgentReachClient:
    def __init__(self, session: Session, *, expected_endpoint: str | None = None) -> None:
        session.scalars(
            select(Setting)
            .where(Setting.key.in_(("config.agent_reach_endpoint", "config.agent_reach_api_key")))
            .order_by(Setting.key)
            .with_for_update(read=True)
        ).all()
        endpoint = get_setting(session, "agent_reach_endpoint")
        token = get_setting(session, "agent_reach_api_key")
        if not endpoint or not token:
            raise RunnerError("Agent Reach runner is not configured")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RunnerError("Agent Reach runner endpoint is invalid")
        self.base_url = endpoint.rstrip("/")
        if expected_endpoint is not None and self.base_url != expected_endpoint.rstrip("/"):
            raise RunnerError("configured runner endpoint differs from the task endpoint")
        self.token = token

    def health(self) -> RunnerHealth:
        status, body = self._request("GET", "/healthz")
        if status != 200:
            raise RunnerError(f"Agent Reach runner returned HTTP {status}")
        try:
            return RunnerHealth.model_validate(json.loads(body))
        except (ValueError, TypeError):
            raise RunnerError("Agent Reach runner returned an invalid health response") from None

    def submit(self, task: AgentReachTask) -> RunnerTaskResponse:
        now = datetime.now(UTC)
        idempotency_key = f"africasignal:agent-reach:{task.id}"
        payload = {
            "task_id": str(task.id),
            "idempotency_key": idempotency_key,
            "control_generation": task.control_revision,
            "config_revision": task.control_revision,
            "capability": "public_search_metadata",
            "topic": task.topic,
            "query": task.query,
            "country": "NG",
            "deadline_at": ((task.started_at or now) + timedelta(minutes=30))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "max_results": task.max_results,
            "max_output_bytes": MAX_RESPONSE_BYTES,
            "policy": {
                "fetch_candidate_pages": False,
                "fetch_transcripts": False,
                "download_attachments": False,
                "publish_or_notify": False,
            },
        }
        status, body = self._request(
            "POST",
            "/v1/tasks",
            json_body=payload,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        if status not in (200, 202):
            raise RunnerError(f"Agent Reach runner returned HTTP {status}")
        return self._parse(body)

    def find_by_task_id(self, task_id: int) -> RunnerTaskResponse | None:
        status, body = self._request("GET", f"/v1/tasks/by-task/{task_id}")
        if status == 404:
            return None
        if status != 200:
            raise RunnerError(f"Agent Reach runner returned HTTP {status}")
        response = self._parse(body)
        if response.task_id != str(task_id):
            raise RunnerError("Agent Reach runner returned a different local task identity")
        return response

    def status(self, external_task_id: str) -> RunnerTaskResponse:
        status, body = self._request("GET", f"/v1/tasks/{quote(external_task_id, safe='')}")
        if status != 200:
            raise RunnerError(f"Agent Reach runner returned HTTP {status}")
        return self._parse(body)

    def cancel(self, external_task_id: str) -> str:
        status, body = self._request("DELETE", f"/v1/tasks/{quote(external_task_id, safe='')}")
        if status in (204, 404):
            return "cancelled"
        if status not in (200, 202):
            raise RunnerError(f"Agent Reach runner returned HTTP {status}")
        response = self._parse(body)
        if response.external_task_id != external_task_id:
            raise RunnerError("Agent Reach runner task identity changed")
        # 202 is only an acknowledgement unless the body confirms a terminal state.
        if response.status == "cancelled":
            return "cancelled"
        if response.status in ("failed", "succeeded", "expired"):
            return "completed"
        return "pending"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        try:
            # The shared transport resolves the configured host once and pins all dials to public
            # addresses. No environment proxy, redirect or second DNS lookup can reach metadata IPs.
            transport = pinned_http_transport(self.base_url)
            with httpx.Client(
                transport=transport,
                timeout=httpx.Timeout(connect=5, read=15, write=5, pool=5),
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                    "User-Agent": "AfricaSignal-AgentReach/1.0",
                    **(extra_headers or {}),
                },
            ) as client:
                with client.stream(method, f"{self.base_url}{path}", json=json_body) as response:
                    status = response.status_code
                    if status not in (200, 202):
                        return status, b""
                    declared = response.headers.get("content-length")
                    if declared and int(declared) > MAX_RESPONSE_BYTES:
                        raise RunnerError("Agent Reach response exceeded its size limit")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise RunnerError("Agent Reach response exceeded its size limit")
                    return status, bytes(body)
        except RunnerError:
            raise
        except (httpx.HTTPError, ValueError, OSError):
            raise RunnerError(
                "Agent Reach runner request failed or its public endpoint is unavailable"
            ) from None

    @staticmethod
    def _parse(body: bytes) -> RunnerTaskResponse:
        try:
            value = json.loads(body)
            return RunnerTaskResponse.model_validate(value)
        except (ValueError, TypeError):
            raise RunnerError("Agent Reach runner returned an invalid task response") from None
