"""Durable research lineage. Source text remains solely in permission-aware evidence storage."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from africasignal.models.base import Base, CreatedMixin

STATES = (
    "open",
    "queued",
    "researching",
    "evidence_ready",
    "validated",
    "applying",
    "verifying",
    "resolved",
    "waiting_permission",
    "waiting_budget",
    "waiting_window",
    "unresolved",
    "escalated",
    "cancelled",
)


class ResearchQuestion(CreatedMixin, Base):
    __tablename__ = "research_question"
    __table_args__ = (
        CheckConstraint(f"state IN {STATES}", name="state_valid"),
        CheckConstraint("fetch_count >= 0 AND model_count >= 0", name="counters_nonnegative"),
        Index("ix_research_question_state_due", "state", "next_due_at"),
    )
    identity: Mapped[str] = mapped_column(Text, unique=True)
    situation_id: Mapped[int] = mapped_column(ForeignKey("situation.id"))
    version_id: Mapped[int] = mapped_column(ForeignKey("assessment_version.id"))
    kind: Mapped[str] = mapped_column(Text)
    generation: Mapped[str] = mapped_column(Text)
    policy_revision: Mapped[int] = mapped_column(Integer)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(Text, default="open")
    reason: Mapped[str] = mapped_column(Text, default="gap_detected")
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetch_count: Mapped[int] = mapped_column(Integer, default=0)
    model_count: Mapped[int] = mapped_column(Integer, default=0)


class ResearchAttempt(CreatedMixin, Base):
    __tablename__ = "research_attempt"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('reserved','completed','captured','failed','permission_changed')",
            name="outcome_valid",
        ),
        Index("ix_research_attempt_question", "question_id", "created_at"),
    )
    question_id: Mapped[int] = mapped_column(ForeignKey("research_question.id"))
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    permission_version: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[int] = mapped_column(BigInteger)
    outcome: Mapped[str] = mapped_column(Text, default="reserved")
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_document.id"))
    # No full URL, title, query, excerpt or provider prose is copied here.
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ResearchDecision(CreatedMixin, Base):
    __tablename__ = "research_decision"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('supported','contradicted','insufficient','out_of_scope')",
            name="verdict_valid",
        ),
    )
    question_id: Mapped[int] = mapped_column(ForeignKey("research_question.id"), unique=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey("evidence_document.id"))
    verdict: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB)


class CorrectionAction(CreatedMixin, Base):
    __tablename__ = "correction_action"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','applying','verifying','verified','blocked')", name="state_valid"
        ),
        Index("ix_correction_action_state", "state", "created_at"),
    )
    question_id: Mapped[int] = mapped_column(ForeignKey("research_question.id"), unique=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("research_decision.id"), unique=True)
    before_version_id: Mapped[int] = mapped_column(ForeignKey("assessment_version.id"))
    after_version_id: Mapped[int | None] = mapped_column(ForeignKey("assessment_version.id"))
    state: Mapped[str] = mapped_column(Text, default="queued")
    reason: Mapped[str] = mapped_column(Text, default="awaiting_application")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
