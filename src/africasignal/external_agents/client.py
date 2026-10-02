"""Strict HTTPS protocol for compatible external-agent services.

The service is distinct from model-provider APIs and Agent Reach. Only public, bounded
metadata tasks are sent; results are untrusted and never enter evidence or publication directly.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from africasignal.models import ExternalAgentProfile, ExternalAgentTask
from africasignal.net.netutil import pinned_http_transport

PROTOCOL_VERSION = "africasignal.external-agent/1"
MAX_RESPONSE_BYTES = 65_536
REQUIRED_ENFORCED_LIMITS = frozenset(
    {
        "allowed_domains",
        "idempotency_key",
        "max_cost_per_task_usd",
        "max_output_bytes",
        "max_spend_per_day_usd",
        "max_steps",
        "timeout_seconds",
    }
)
AgentPurpose = Literal["research", "summarize", "classify"]
RemoteStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "expired"]


class ExternalAgentProtocolError(RuntimeError):
    """A sanitized failure in the configured external-agent protocol."""


class AmbiguousExternalAgentOutcome(ExternalAgentProtocolError):
    """A submit may have reached the remote service and must be reconciled by idempotency key."""


class AgentHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    protocol_version: Literal["africasignal.external-agent/1"]
    agent_id: str = Field(min_length=1, max_length=64)
    capabilities: list[AgentPurpose] = Field(min_length=1, max_length=3)
    enforced_limits: list[str] = Field(min_length=1, max_length=16)
    max_concurrency: int = Field(ge=1, le=4)

    @field_validator("capabilities", "enforced_limits")
    @classmethod
    def unique_values(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("health values must be unique")
        return value


class ReportedUsage(BaseModel):
    """Remote-reported telemetry only; never used to release a local spend reservation."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    output_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    reported_cost_usd: Decimal | None = Field(default=None, ge=0, le=Decimal("1000000"))


class AgentTaskReply(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    protocol_version: Literal["africasignal.external-agent/1"]
    task_id: str = Field(min_length=1, max_length=20, pattern=r"^[0-9]+$")
    external_task_id: str = Field(min_length=1, max_length=200)
    status: RemoteStatus
    result_text: str | None = Field(default=None, max_length=MAX_RESPONSE_BYTES)
    error: str | None = Field(default=None, max_length=300)
    usage: ReportedUsage | None = None

    @model_validator(mode="after")
    def validate_terminal_result(self) -> AgentTaskReply:
        if self.status == "succeeded" and self.result_text is None:
            raise ValueError("succeeded tasks must include a result")
        if self.status != "succeeded" and self.result_text is not None:
            raise ValueError("results are allowed only on succeeded tasks")
        return self


class ExternalAgentClient:
    """A no-redirect, public-address-pinned client for the versioned HTTPS task protocol."""

    def __init__(self, profile: ExternalAgentProfile, token: str) -> None:
        parts = urlsplit(profile.endpoint_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ExternalAgentProtocolError("External-agent HTTPS endpoint is invalid")
        if not token or any(ch.isspace() for ch in token):
            raise ExternalAgentProtocolError("External-agent credential is unavailable")
        self.base_url = profile.endpoint_url.rstrip("/")
        self.profile = profile
        self.token = token

    def health(self) -> AgentHealth:
        status, body = self._request("GET", "/healthz")
        if status != 200:
            raise ExternalAgentProtocolError(f"External agent returned HTTP {status}")
        try:
            health = AgentHealth.model_validate(json.loads(body))
        except (ValueError, TypeError):
            raise ExternalAgentProtocolError(
                "External agent returned invalid health data"
            ) from None
        if health.agent_id != self.profile.slug:
            raise ExternalAgentProtocolError("External agent identity did not match its profile")
        if not set(health.capabilities).issubset(set(self.profile.allowed_purposes)):
            raise ExternalAgentProtocolError("External agent advertises an unconfigured capability")
        if not REQUIRED_ENFORCED_LIMITS.issubset(set(health.enforced_limits)):
            raise ExternalAgentProtocolError(
                "External agent does not declare every required task limit"
            )
        # AfricaSignal applies the lower of the local and remote concurrency caps at admission.
        return health

    def submit(self, task: ExternalAgentTask, profile: ExternalAgentProfile) -> AgentTaskReply:
        payload: dict[str, object] = {
            "protocol_version": PROTOCOL_VERSION,
            "task_id": str(task.id),
            "idempotency_key": task.idempotency_key,
            "purpose": task.purpose,
            "objective": task.objective,
            "data_classification": "public_metadata",
            "allowed_domains": profile.allowed_domains,
            "limits": {
                "max_steps": profile.max_steps,
                "timeout_seconds": profile.timeout_seconds,
                "max_output_bytes": profile.max_output_bytes,
                "max_cost_per_task_usd": str(profile.max_cost_per_task_usd),
                "max_spend_per_day_usd": str(profile.max_spend_per_day_usd),
                "deadline_at": task.deadline_at.isoformat(),
            },
        }
        try:
            status, body = self._request(
                "POST",
                "/v1/tasks",
                json_body=payload,
                extra_headers={"Idempotency-Key": task.idempotency_key},
            )
        except ExternalAgentProtocolError as exc:
            raise AmbiguousExternalAgentOutcome(
                "External-agent submission outcome is uncertain; reconciliation is required"
            ) from exc
        if status not in (200, 202):
            # A non-success response can still follow a remote commit. The server contract requires
            # idempotency; reconcile before another submission for every ambiguous HTTP result.
            if status >= 500 or status == 429:
                raise AmbiguousExternalAgentOutcome(
                    f"External-agent submission outcome is uncertain (HTTP {status})"
                )
            raise ExternalAgentProtocolError(f"External agent rejected the task (HTTP {status})")
        return self._parse_reply(body, task, profile)

    def status(
        self, external_task_id: str, task: ExternalAgentTask, profile: ExternalAgentProfile
    ) -> AgentTaskReply:
        path = f"/v1/tasks/{quote(external_task_id, safe='')}"
        status, body = self._request("GET", path)
        if status != 200:
            raise ExternalAgentProtocolError(f"External agent returned HTTP {status}")
        return self._parse_reply(body, task, profile)

    def by_idempotency_key(
        self, task: ExternalAgentTask, profile: ExternalAgentProfile
    ) -> AgentTaskReply | None:
        path = f"/v1/tasks/by-idempotency/{quote(task.idempotency_key, safe='')}"
        status, body = self._request("GET", path)
        if status == 404:
            return None
        if status != 200:
            raise ExternalAgentProtocolError(f"External agent returned HTTP {status}")
        return self._parse_reply(body, task, profile)

    def cancel(
        self, external_task_id: str, task: ExternalAgentTask, profile: ExternalAgentProfile
    ) -> AgentTaskReply | None:
        path = f"/v1/tasks/{quote(external_task_id, safe='')}"
        status, body = self._request("DELETE", path)
        if status == 204:
            return None
        if status not in (200, 202):
            raise ExternalAgentProtocolError(f"External agent returned HTTP {status}")
        return self._parse_reply(body, task, profile)

    def _parse_reply(
        self, body: bytes, task: ExternalAgentTask, profile: ExternalAgentProfile
    ) -> AgentTaskReply:
        try:
            reply = AgentTaskReply.model_validate(json.loads(body))
        except (ValueError, TypeError):
            raise ExternalAgentProtocolError(
                "External agent returned an invalid task response"
            ) from None
        if reply.task_id != str(task.id):
            raise ExternalAgentProtocolError("External agent returned a different task identity")
        if (
            reply.result_text is not None
            and len(reply.result_text.encode("utf-8")) > profile.max_output_bytes
        ):
            raise ExternalAgentProtocolError(
                "External agent result exceeded the configured byte limit"
            )
        return reply

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        try:
            transport = pinned_http_transport(self.base_url)
            with httpx.Client(
                transport=transport,
                timeout=httpx.Timeout(connect=5, read=15, write=5, pool=5),
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                    "User-Agent": "AfricaSignal-ExternalAgent/1.0",
                    **(extra_headers or {}),
                },
            ) as client:
                with client.stream(method, f"{self.base_url}{path}", json=json_body) as response:
                    status = response.status_code
                    if status not in (200, 202):
                        return status, b""
                    declared = response.headers.get("content-length")
                    if declared and int(declared) > MAX_RESPONSE_BYTES:
                        raise ExternalAgentProtocolError(
                            "External-agent response exceeded its size limit"
                        )
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ExternalAgentProtocolError(
                                "External-agent response exceeded its size limit"
                            )
                    return status, bytes(body)
        except ExternalAgentProtocolError:
            raise
        except (httpx.TimeoutException, httpx.RequestError, OSError, ValueError):
            raise ExternalAgentProtocolError("External-agent HTTPS request failed") from None
