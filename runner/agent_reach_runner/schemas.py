"""Schemas for the isolated AfricaSignal discovery runner."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL_VERSION = "africasignal-agent-reach/1"
MAX_OUTPUT_BYTES = 128_000


class TaskPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fetch_candidate_pages: Literal[False]
    fetch_transcripts: Literal[False]
    download_attachments: Literal[False]
    publish_or_notify: Literal[False]


class TaskSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task_id: str = Field(min_length=1, max_length=20, pattern=r"^[0-9]+$")
    idempotency_key: str = Field(min_length=1, max_length=120)
    control_generation: int = Field(ge=1)
    config_revision: int = Field(ge=1)
    capability: Literal["public_search_metadata"]
    topic: Literal["energy", "food"]
    query: str = Field(min_length=8, max_length=500)
    country: Literal["NG"]
    deadline_at: datetime
    max_results: int = Field(ge=1, le=20)
    max_output_bytes: int = Field(ge=1024, le=MAX_OUTPUT_BYTES)
    policy: TaskPolicy

    @field_validator("query")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("query contains a control character")
        return value

    @model_validator(mode="after")
    def validate_deadline_and_idempotency(self) -> TaskSubmission:
        # Persisted requests are revalidated after restart; the queue marks stale deadlines expired.
        if self.deadline_at.tzinfo is None:
            raise ValueError("deadline_at must include a timezone")
        expected = f"africasignal:agent-reach:{self.task_id}"
        if self.idempotency_key != expected:
            raise ValueError("idempotency_key does not match task_id")
        return self


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


class TaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    external_task_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=20)
    control_generation: int = Field(ge=1)
    config_revision: int = Field(ge=1)
    status: Literal["queued", "running", "succeeded", "failed", "cancelled", "expired"]
    results: list[RunnerCandidate] = Field(default_factory=list, max_length=20)
    error: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def results_only_when_done(self) -> TaskResponse:
        if self.status != "succeeded" and self.results:
            raise ValueError("candidates may be returned only for a succeeded task")
        return self


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    protocol_version: Literal["africasignal-agent-reach/1"]
    capabilities: list[Literal["public_search_metadata"]] = Field(default_factory=list)
    backend: Literal["youtube_data_api_v3"] = "youtube_data_api_v3"
    max_concurrency: int = Field(ge=1, le=4)


def utc_now() -> datetime:
    return datetime.now(UTC)
