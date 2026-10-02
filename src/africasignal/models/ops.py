"""B3.8 Feedback, outbox, jobs, metrics, operators, audit; B4 job table."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from africasignal.models.base import Base, CreatedMixin, pg_enum

feedback_kind = pg_enum("feedback_kind", "useful_yes", "useful_no", "error_report")
feedback_status = pg_enum(
    "feedback_status",
    "received",
    "triaged",
    "investigating",
    "resolved_updated",
    "resolved_no_change",
    "resolved_insufficient",
    "closed",
)
outbox_kind = pg_enum("outbox_kind", "email_login", "email_digest", "email_correction")
outbox_status = pg_enum("outbox_status", "pending", "sent", "failed", "dead")
job_status = pg_enum("job_status", "queued", "running", "done", "failed", "dead")
operator_role = pg_enum("operator_role", "admin", "editor")
channel_post_channel = pg_enum("channel_post_channel", "wa", "x")


class Feedback(CreatedMixin, Base):
    __tablename__ = "feedback"

    assessment_version_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_version.id"), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    anon_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(feedback_kind, nullable=False)
    text: Mapped[str | None] = mapped_column(Text)  # max 2000 chars, enforced at the edge
    contact_email: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(feedback_status, nullable=False, server_default="received")
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Outbox(CreatedMixin, Base):
    __tablename__ = "outbox"

    kind: Mapped[str] = mapped_column(outbox_kind, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(outbox_status, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text)


class Job(CreatedMixin, Base):
    __tablename__ = "job"
    __table_args__ = (Index("ix_job_status_run_at", "status", "run_at"),)

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(job_status, nullable=False, server_default="queued")
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    locked_by: Mapped[str | None] = mapped_column(Text)
    # Worker ids are reused across claims. This unique token fences stale attempts, including
    # a reclaim by the same worker process.
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LlmBudgetReservation(CreatedMixin, Base):
    """Durable upper-bound reservations that prevent concurrent model calls exceeding the cap."""

    __tablename__ = "llm_budget_reservation"
    __table_args__ = (
        CheckConstraint("state IN ('reserved', 'uncertain', 'settled')", name="state_valid"),
        CheckConstraint("reserved_usd >= 0", name="amount_nonnegative"),
        CheckConstraint("input_token_bound >= 0", name="input_nonnegative"),
        CheckConstraint("output_token_bound >= 0", name="output_nonnegative"),
        Index("ix_llm_budget_reservation_day_state", "budget_day", "state"),
        Index("ix_llm_budget_reservation_job_state", "job_id", "state"),
    )

    budget_day: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("job.id", ondelete="SET NULL"))
    input_token_bound: Mapped[int] = mapped_column(Integer, nullable=False)
    output_token_bound: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(10, 5), nullable=False)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 5))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="reserved")
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Event(CreatedMixin, Base):
    """Product metrics. Retention 13 months. No IP address is stored."""

    __tablename__ = "event"
    __table_args__ = (Index("ix_event_name_ts", "name", "ts"),)

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    anon_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    situation_id: Mapped[int | None] = mapped_column(ForeignKey("situation.id"))
    ref: Mapped[str | None] = mapped_column(Text)  # for example wa, x, email
    props: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")


class Operator(CreatedMixin, Base):
    __tablename__ = "operator"

    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)  # argon2id
    totp_secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(operator_role, nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Raised on sign-out, revocation and password change; a console cookie carries the value it was
    # issued under and stops working when the value moves on.
    session_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The newest TOTP time step accepted, so a code cannot be used twice (across processes too).
    last_totp_step: Mapped[int | None] = mapped_column(BigInteger)


class OperatorSignInFailure(CreatedMixin, Base):
    """One failed console sign-in, for the throttle. ``client_key`` is a keyed hash of the
    client's address, never the address. Rows older than the throttle window are deleted."""

    __tablename__ = "operator_sign_in_failure"
    __table_args__ = (Index("ix_operator_sign_in_failure_email_at", "email", "at"),)

    email: Mapped[str] = mapped_column(Text, nullable=False)
    client_key: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLog(CreatedMixin, Base):
    __tablename__ = "audit_log"

    # Empty for a change the system made itself (a job raising an alert); see audit.record_system.
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("operator.id"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LlmCall(CreatedMixin, Base):
    __tablename__ = "llm_call"

    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 5), nullable=False)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("job.id"))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class ChannelPost(CreatedMixin, Base):
    """An operator's record that they posted a draft by hand on WhatsApp or X (AS-033, demand
    test D1). The app sends nothing: this row only remembers that a person did, which version, when
    and by whom. One row per version and channel."""

    __tablename__ = "channel_post"
    __table_args__ = (UniqueConstraint("assessment_version_id", "channel"),)

    assessment_version_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_version.id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(channel_post_channel, nullable=False)
    posted_by_operator_id: Mapped[int] = mapped_column(ForeignKey("operator.id"), nullable=False)
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    post_url: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class WorkloadControl(Base):
    """Versioned runtime switch and schedule for one resource-intensive workload."""

    __tablename__ = "workload_control"
    __table_args__ = (
        CheckConstraint("name IN ('ai', 'agent_reach', 'processing')", name="name_valid"),
        CheckConstraint("revision > 0", name="revision_positive"),
    )

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    schedule: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentReachTask(CreatedMixin, Base):
    """A bounded discovery request sent to an isolated Agent Reach bridge."""

    __tablename__ = "agent_reach_task"
    __table_args__ = (
        CheckConstraint("topic IN ('energy', 'food')", name="topic_valid"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancellation_requested', "
            "'cancelled', 'expired', 'outcome_unknown')",
            name="status_valid",
        ),
        CheckConstraint("max_results BETWEEN 1 AND 20", name="max_results_bounds"),
        CheckConstraint("control_revision > 0", name="control_revision_positive"),
        Index("ix_agent_reach_task_status_created", "status", "created_at"),
        UniqueConstraint("external_task_id", name="external_task_id_unique"),
    )

    requested_by_operator_id: Mapped[int] = mapped_column(ForeignKey("operator.id"), nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    max_results: Mapped[int] = mapped_column(Integer, nullable=False)
    control_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    runner_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    external_task_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentReachCandidate(CreatedMixin, Base):
    """Metadata-only discovery result awaiting a human decision."""

    __tablename__ = "agent_reach_candidate"
    __table_args__ = (
        UniqueConstraint("task_id", "canonical_url", name="task_canonical_url_unique"),
        CheckConstraint(
            "status IN ('pending', 'rejected', 'fetch_queued')",
            name="status_valid",
        ),
        Index("ix_agent_reach_candidate_status_created", "status", "created_at"),
    )

    task_id: Mapped[int] = mapped_column(
        ForeignKey("agent_reach_task.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    backend: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    queued_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("source.id", ondelete="SET NULL")
    )
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Setting(Base):
    """Key/value settings, including ``publication_suspended``. Keyed by name, no id column."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
