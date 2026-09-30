"""B3.6 Situations and assessments."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from africasignal.models.base import Base, CreatedMixin, pg_enum, topic_enum

situation_kind = pg_enum("situation_kind", "price_series", "policy")
situation_status = pg_enum("situation_status", "active", "dormant", "closed")
assessment_template = pg_enum("assessment_template", "T1_price_change", "T2_policy_change")
assessment_status = pg_enum(
    "assessment_status",
    "draft",
    "published",
    "withheld",
    "stale",
    "superseded",
    "withdrawn",
)
evidence_state_enum = pg_enum(
    "evidence_state", "reported", "corroborated", "disputed", "insufficient"
)
severity_enum = pg_enum("severity", "none", "low", "medium", "high")
assessment_input_kind = pg_enum(
    "assessment_input_kind", "measurement", "claim", "evidence_document"
)


class Situation(CreatedMixin, Base):
    __tablename__ = "situation"

    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(situation_kind, nullable=False)
    topic: Mapped[str] = mapped_column(topic_enum, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    item_code: Mapped[str | None] = mapped_column(Text)
    policy_series: Mapped[str | None] = mapped_column(Text)
    place_id: Mapped[int] = mapped_column(ForeignKey("place.id"), nullable=False)  # scope
    status: Mapped[str] = mapped_column(situation_status, nullable=False, server_default="active")
    # use_alter: situation and assessment_version reference each other.
    current_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("assessment_version.id", use_alter=True, name="fk_situation_current_version"),
    )


class AssessmentVersion(CreatedMixin, Base):
    __tablename__ = "assessment_version"
    __table_args__ = (UniqueConstraint("situation_id", "version"),)

    situation_id: Mapped[int] = mapped_column(ForeignKey("situation.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    template: Mapped[str] = mapped_column(assessment_template, nullable=False)
    template_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)  # publication policy version
    model_id: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    inputs_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(assessment_status, nullable=False)
    evidence_state: Mapped[str] = mapped_column(evidence_state_enum, nullable=False)
    severity: Mapped[str] = mapped_column(severity_enum, nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    facts: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    explanation: Mapped[str | None] = mapped_column(Text)
    possible_factors: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    unknowns: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    scope_label: Mapped[str] = mapped_column(Text, nullable=False)
    period_label: Mapped[str] = mapped_column(Text, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    change_summary: Mapped[str | None] = mapped_column(Text)
    withheld_reasons: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("assessment_version.id"))


class AssessmentInput(CreatedMixin, Base):
    __tablename__ = "assessment_input"
    __table_args__ = (Index("ix_assessment_input_kind_id", "input_kind", "input_id"),)

    assessment_version_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_version.id"), nullable=False
    )
    input_kind: Mapped[str] = mapped_column(assessment_input_kind, nullable=False)
    input_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
