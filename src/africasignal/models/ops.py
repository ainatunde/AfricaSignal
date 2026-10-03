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
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
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
    __table_args__ = (
        Index("ix_job_status_run_at", "status", "run_at"),
        Index(
            "ix_job_finished_retention",
            "finished_at",
            postgresql_where=text("status IN ('done', 'dead')"),
        ),
    )

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
    job_id: Mapped[int | None] = mapped_column(ForeignKey("job.id", ondelete="SET NULL"))
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
        CheckConstraint(
            "name IN ('ai', 'agent_reach', 'external_agents', 'processing', 'commercial')",
            name="name_valid",
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
    )

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    schedule: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CommercialControl(Base):
    """Versioned default-off public and AI commercial switches."""

    __tablename__ = "commercial_control"
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="singleton_only"),
        CheckConstraint("revision > 0", name="revision_positive"),
    )

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    global_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    explore_sponsorship_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    context_ai_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    updated_by_operator_id: Mapped[int | None] = mapped_column(ForeignKey("operator.id"))
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


class ExternalAgentProfile(Base):
    """Operator-configured HTTPS agent with a bounded, versioned execution contract."""

    __tablename__ = "external_agent_profile"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug_valid"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("max_steps BETWEEN 1 AND 10", name="max_steps_bounds"),
        CheckConstraint("timeout_seconds BETWEEN 1 AND 300", name="timeout_bounds"),
        CheckConstraint("max_output_bytes BETWEEN 1024 AND 65536", name="output_bounds"),
        CheckConstraint("max_tasks_per_day BETWEEN 1 AND 100", name="daily_tasks_bounds"),
        CheckConstraint("max_concurrency BETWEEN 1 AND 4", name="concurrency_bounds"),
        CheckConstraint("max_cost_per_task_usd > 0", name="task_cost_positive"),
        CheckConstraint("max_spend_per_day_usd >= max_cost_per_task_usd", name="daily_cost_bounds"),
        Index("ix_external_agent_profile_enabled", "enabled", "created_at"),
    )

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    credential_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_purposes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    allowed_domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    max_tasks_per_day: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    max_cost_per_task_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    max_spend_per_day_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_token_fingerprint: Mapped[str | None] = mapped_column(Text)
    health_capabilities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    health_enforced_limits: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    health_max_concurrency: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExternalAgentTask(CreatedMixin, Base):
    """Durable, supervised task sent to one compatible external-agent profile."""

    __tablename__ = "external_agent_task"
    __table_args__ = (
        CheckConstraint("purpose IN ('research', 'summarize', 'classify')", name="purpose_valid"),
        CheckConstraint(
            "status IN ('pending', 'admitted', 'running', 'cancellation_requested', "
            "'succeeded', 'failed', 'expired', 'cancelled', 'outcome_unknown')",
            name="status_valid",
        ),
        CheckConstraint("profile_revision > 0", name="profile_revision_positive"),
        CheckConstraint("workload_revision > 0", name="workload_revision_positive"),
        CheckConstraint("reserved_cost_usd > 0", name="reserved_cost_positive"),
        UniqueConstraint("idempotency_key", name="idempotency_key_unique"),
        UniqueConstraint("profile_slug", "external_task_id", name="profile_external_task_unique"),
        Index("ix_external_agent_task_status_created", "status", "created_at"),
        Index("ix_external_agent_task_profile_status", "profile_slug", "status", "created_at"),
    )

    profile_slug: Mapped[str] = mapped_column(
        ForeignKey("external_agent_profile.slug", ondelete="RESTRICT"), nullable=False
    )
    requested_by_operator_id: Mapped[int] = mapped_column(ForeignKey("operator.id"), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    profile_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    workload_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    external_task_id: Mapped[str | None] = mapped_column(Text)
    result_text: Mapped[str | None] = mapped_column(Text)
    reported_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    """Key/value settings, including ``publication_suspended``. Keyed by name, no id column."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Sponsor(Base):
    """Reviewed business identity for first-party topic sponsorship."""

    __tablename__ = "sponsor"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review', 'approved', 'paused', 'retired')", name="status_valid"
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("length(public_name) BETWEEN 1 AND 100", name="public_name_bounds"),
        CheckConstraint("length(website_url) BETWEEN 12 AND 500", name="website_url_bounds"),
        CheckConstraint("length(contact_email) BETWEEN 3 AND 254", name="contact_email_bounds"),
        CheckConstraint(
            "category IN ('energy_provider', 'energy_efficiency', 'food_retailer', "
            "'agriculture', 'general_business', 'unclassified')",
            name="category_valid",
        ),
        Index("ix_sponsor_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_name: Mapped[str] = mapped_column(Text, nullable=False)
    website_url: Mapped[str] = mapped_column(Text, nullable=False)
    contact_email: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False, server_default="unclassified")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending_review")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by_operator_id: Mapped[int] = mapped_column(ForeignKey("operator.id"), nullable=False)
    updated_by_operator_id: Mapped[int] = mapped_column(ForeignKey("operator.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Campaign(Base):
    """Versioned direct-sponsorship agreement; amounts are integer kobo in NGN."""

    __tablename__ = "campaign"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'approved', 'active', 'paused', 'ended')", name="status_valid"
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("currency = 'NGN'", name="currency_supported"),
        CheckConstraint(
            "agreed_fee_minor IS NULL OR agreed_fee_minor >= 0", name="fee_nonnegative"
        ),
        UniqueConstraint("sponsor_id", "internal_name", name="sponsor_internal_name_unique"),
        Index("ix_campaign_status_updated", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sponsor_id: Mapped[int] = mapped_column(
        ForeignKey("sponsor.id", ondelete="RESTRICT"), nullable=False
    )
    internal_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default="NGN")
    agreed_fee_minor: Mapped[int | None] = mapped_column(BigInteger)
    agreement_reference: Mapped[str | None] = mapped_column(Text)
    created_by_operator_id: Mapped[int] = mapped_column(ForeignKey("operator.id"), nullable=False)
    approved_by_operator_id: Mapped[int | None] = mapped_column(ForeignKey("operator.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreativeVersion(Base):
    """Immutable reviewed creative content once approved; edits create another version."""

    __tablename__ = "creative_version"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint(
            "status IN ('draft', 'approved', 'rejected', 'withdrawn')", name="status_valid"
        ),
        UniqueConstraint("campaign_id", "version", name="campaign_version_unique"),
        Index("ix_creative_version_campaign_status", "campaign_id", "status", "version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaign.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    asset_key: Mapped[str | None] = mapped_column(Text)
    alt_text: Mapped[str | None] = mapped_column(Text)
    destination_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    reviewer_operator_id: Mapped[int | None] = mapped_column(ForeignKey("operator.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_operator_id: Mapped[int] = mapped_column(ForeignKey("operator.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ContentContext(Base):
    """Commercial-only context projection bound to an exact published assessment revision."""

    __tablename__ = "content_context"
    __table_args__ = (
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_sha256"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint(
            "length(taxonomy_version) BETWEEN 1 AND 80", name="taxonomy_version_bounds"
        ),
        CheckConstraint(
            "length(classifier_version) BETWEEN 1 AND 80", name="classifier_version_bounds"
        ),
        CheckConstraint(
            "suitability IN ('eligible', 'restricted', 'unknown', 'invalidated')",
            name="suitability_valid",
        ),
        UniqueConstraint(
            "assessment_version_id",
            "content_hash",
            "taxonomy_version",
            "classifier_version",
            "revision",
            name="assessment_taxonomy_classifier_unique",
        ),
        Index("ix_content_context_assessment_suitability", "assessment_version_id", "suitability"),
        Index("ix_content_context_expiry", "expires_at"),
        Index("ix_content_context_source_refs", "source_refs", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    assessment_version_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_version.id", ondelete="RESTRICT"), nullable=False
    )
    canonical_place_id: Mapped[int] = mapped_column(
        ForeignKey("place.id", ondelete="RESTRICT"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(Text, nullable=False)
    classifier_version: Mapped[str] = mapped_column(Text, nullable=False)
    topic_tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    source_refs: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    suitability: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidation_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlacementBooking(Base):
    """Exclusive topic-scoped direct sponsorship interval on the Explore surface."""

    __tablename__ = "placement_booking"
    __table_args__ = (
        CheckConstraint("surface = 'explore_topic'", name="surface_allowed"),
        CheckConstraint("topic IN ('energy', 'food')", name="topic_allowed"),
        CheckConstraint("ends_at > starts_at", name="interval_positive"),
        CheckConstraint("exclusive IS TRUE", name="exclusive_only"),
        CheckConstraint(
            "status IN ('draft', 'approved', 'active', 'paused', 'ended')", name="status_valid"
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        Index(
            "ix_booking_scope_status_interval", "surface", "topic", "status", "starts_at", "ends_at"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaign.id", ondelete="RESTRICT"), nullable=False
    )
    surface: Mapped[str] = mapped_column(Text, nullable=False, server_default="explore_topic")
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    creative_version_id: Mapped[int] = mapped_column(
        ForeignKey("creative_version.id", ondelete="RESTRICT"), nullable=False
    )
    exclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by_operator_id: Mapped[int] = mapped_column(ForeignKey("operator.id"), nullable=False)
    approved_by_operator_id: Mapped[int | None] = mapped_column(ForeignKey("operator.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_by_operator_id: Mapped[int | None] = mapped_column(ForeignKey("operator.id"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DeliveryEvent(Base):
    """Minimal first-party event record without reader or request identifiers."""

    __tablename__ = "delivery_event"
    __table_args__ = (
        CheckConstraint("event_schema_version > 0", name="event_schema_positive"),
        CheckConstraint("booking_revision > 0", name="booking_revision_positive"),
        CheckConstraint("metric_version > 0", name="metric_version_positive"),
        CheckConstraint(
            "metric IN ('eligible_opportunity', 'server_render', 'click')", name="metric_valid"
        ),
        CheckConstraint("deduplication_hash ~ '^[0-9a-f]{64}$'", name="deduplication_hash_sha256"),
        CheckConstraint(
            "validity_status IN ('accepted', 'rejected')", name="validity_status_valid"
        ),
        CheckConstraint(
            "(validity_status = 'accepted' AND rejection_reason IS NULL) OR "
            "(validity_status = 'rejected' AND rejection_reason IS NOT NULL)",
            name="rejection_reason_matches_status",
        ),
        UniqueConstraint("deduplication_hash", name="deduplication_hash_unique"),
        Index("ix_delivery_event_booking_metric_received", "booking_id", "metric", "received_at"),
        Index("ix_delivery_event_retention", "retention_until"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("placement_booking.id", ondelete="RESTRICT"), nullable=False
    )
    creative_version_id: Mapped[int] = mapped_column(
        ForeignKey("creative_version.id", ondelete="RESTRICT"), nullable=False
    )
    booking_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    event_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    metric_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    deduplication_hash: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    validity_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="accepted")
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeliveryAggregate(Base):
    """Rebuildable UTC-day totals with explicit completeness and metric definition versions."""

    __tablename__ = "delivery_aggregate"
    __table_args__ = (
        CheckConstraint("metric_version > 0", name="metric_version_positive"),
        CheckConstraint(
            "metric IN ('eligible_opportunity', 'server_render', 'click')", name="metric_valid"
        ),
        CheckConstraint("count >= 0", name="count_nonnegative"),
        CheckConstraint(
            "completeness IN ('complete', 'partial', 'unavailable')", name="completeness_valid"
        ),
        UniqueConstraint(
            "booking_id",
            "creative_version_id",
            "metric",
            "metric_version",
            "period_start",
            name="booking_creative_metric_period_unique",
        ),
        Index("ix_delivery_aggregate_period", "period_start", "metric"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("placement_booking.id", ondelete="RESTRICT"), nullable=False
    )
    creative_version_id: Mapped[int] = mapped_column(
        ForeignKey("creative_version.id", ondelete="RESTRICT"), nullable=False
    )
    surface: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    metric_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    completeness: Mapped[str] = mapped_column(Text, nullable=False, server_default="complete")
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rebuilt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CommercialDraft(Base):
    """Immutable revision-bound package proposal; approval never creates a booking."""

    __tablename__ = "commercial_draft"
    __table_args__ = (
        CheckConstraint("kind = 'package'", name="kind_valid"),
        CheckConstraint("status IN ('draft', 'approved', 'rejected')", name="status_valid"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("input_hash ~ '^[0-9a-f]{64}$'", name="input_hash_sha256"),
        UniqueConstraint("kind", "input_hash", name="kind_input_hash_unique"),
        Index("ix_commercial_draft_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaign.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="package")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    input_refs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_by_operator_id: Mapped[int] = mapped_column(ForeignKey("operator.id"), nullable=False)
    reviewed_by_operator_id: Mapped[int | None] = mapped_column(ForeignKey("operator.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
