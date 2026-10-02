"""The response cache, keyed by ``(purpose, prompt_version, model_id, input_sha256)`` (AS-020)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from africasignal.models import LlmResponseCache


def input_sha256(
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    semantic_config: dict[str, Any] | None = None,
) -> str:
    """Hash of everything that decides the answer: both prompts and the response schema."""
    canonical = json.dumps(
        {
            "system": system,
            "user": user,
            "schema": schema,
            "semantic_config": semantic_config or {},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get(
    session: Session, purpose: str, prompt_version: str, model_id: str, sha: str
) -> LlmResponseCache | None:
    return session.scalars(
        select(LlmResponseCache).where(
            LlmResponseCache.purpose == purpose,
            LlmResponseCache.prompt_version == prompt_version,
            LlmResponseCache.model_id == model_id,
            LlmResponseCache.input_sha256 == sha,
        )
    ).first()


def put(
    session: Session,
    purpose: str,
    prompt_version: str,
    model_id: str,
    sha: str,
    response: dict[str, Any],
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Store a response. A concurrent worker that stored the same key first wins; both answers
    were valid for the same input, so either is fine."""
    session.execute(
        insert(LlmResponseCache)
        .values(
            purpose=purpose,
            prompt_version=prompt_version,
            model_id=model_id,
            input_sha256=sha,
            response=response,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        .on_conflict_do_nothing(
            index_elements=["purpose", "prompt_version", "model_id", "input_sha256"]
        )
    )
