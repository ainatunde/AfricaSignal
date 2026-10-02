"""Bounded, typed contracts for revision-bound commercial content context."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Topic = Literal["energy", "food"]
Suitability = Literal["eligible", "restricted", "unknown", "invalidated"]
ReasonCode = Literal[
    "current_public",
    "not_current",
    "not_published",
    "withdrawn_content",
    "expired",
    "publication_suspended",
    "closed_situation",
    "unsupported_topic",
    "disputed_evidence",
    "insufficient_evidence",
    "missing_evidence",
    "withdrawn_evidence",
    "permission_unavailable",
    "permission_denied",
    "taxonomy_mismatch",
    "stale_context",
]


class ContextRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment_version_id: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ContextDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    topic_tags: tuple[Topic, ...] = Field(min_length=1, max_length=2)
    canonical_place_id: int = Field(ge=1)
    taxonomy_version: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    classifier_version: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    suitability: Literal["eligible", "restricted", "unknown"]
    reason_codes: tuple[ReasonCode, ...] = Field(min_length=1, max_length=8)
    evidence_refs: tuple[int, ...] = Field(max_length=100)
    expires_at: datetime

    @field_validator("topic_tags")
    @classmethod
    def unique_topics(cls, value: tuple[Topic, ...]) -> tuple[Topic, ...]:
        if len(set(value)) != len(value):
            raise ValueError("topic tags must be unique")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(item < 1 for item in value) or len(set(value)) != len(value):
            raise ValueError("evidence references must be unique positive identifiers")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("context expiry must include a timezone")
        return value


class ContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_ref: ContextRef
    topic: Topic
    canonical_place_id: int
    current: bool
    situation_status: str
    status: str
    evidence_state: str
    published_at: datetime | None
    valid_until: datetime | None
    evidence_refs: tuple[int, ...]
