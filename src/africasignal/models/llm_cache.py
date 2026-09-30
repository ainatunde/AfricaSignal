"""Cache of language-model responses (AS-020). Not in the B3 data model: added in migration 0004."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from africasignal.models.base import Base, CreatedMixin


class LlmResponseCache(CreatedMixin, Base):
    """One validated response per ``(purpose, prompt_version, model_id, input_sha256)``.

    ``input_sha256`` covers the system prompt, the user message and the response schema, so the
    same input is never paid for twice and a changed prompt or schema never reads a stale answer.
    """

    __tablename__ = "llm_response_cache"
    __table_args__ = (UniqueConstraint("purpose", "prompt_version", "model_id", "input_sha256"),)

    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    input_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
