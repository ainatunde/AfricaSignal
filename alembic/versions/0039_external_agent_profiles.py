"""Add configurable external-agent profiles and supervised durable tasks.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_workload_control_name_valid"), "workload_control", type_="check")
    op.create_check_constraint(
        op.f("ck_workload_control_name_valid"),
        "workload_control",
        "name IN ('ai', 'agent_reach', 'external_agents', 'processing')",
    )
    op.execute(
        """
        INSERT INTO workload_control (name, enabled, schedule, revision) VALUES
          ('external_agents', false, jsonb_build_object(
              'timezone', 'Africa/Lagos',
              'windows', jsonb_build_array(jsonb_build_object(
                  'days', jsonb_build_array(0, 1, 2, 3, 4, 5, 6),
                  'start', '01:00', 'end', '04:00'
              )),
              'max_concurrency', 1, 'max_items_per_run', 5
          ), 1)
        """
    )
    op.create_table(
        "external_agent_profile",
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("endpoint_url", sa.Text(), nullable=False),
        sa.Column("credential_ciphertext", sa.Text(), nullable=False),
        sa.Column("allowed_purposes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("allowed_domains", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_output_bytes", sa.Integer(), nullable=False),
        sa.Column("max_tasks_per_day", sa.Integer(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("max_cost_per_task_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("max_spend_per_day_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health_token_fingerprint", sa.Text(), nullable=True),
        sa.Column(
            "health_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "health_enforced_limits",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("health_max_concurrency", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name=op.f("ck_external_agent_profile_slug_valid"),
        ),
        sa.CheckConstraint(
            "revision > 0", name=op.f("ck_external_agent_profile_revision_positive")
        ),
        sa.CheckConstraint(
            "max_steps BETWEEN 1 AND 10", name=op.f("ck_external_agent_profile_max_steps_bounds")
        ),
        sa.CheckConstraint(
            "timeout_seconds BETWEEN 1 AND 300",
            name=op.f("ck_external_agent_profile_timeout_bounds"),
        ),
        sa.CheckConstraint(
            "max_output_bytes BETWEEN 1024 AND 65536",
            name=op.f("ck_external_agent_profile_output_bounds"),
        ),
        sa.CheckConstraint(
            "max_tasks_per_day BETWEEN 1 AND 100",
            name=op.f("ck_external_agent_profile_daily_tasks_bounds"),
        ),
        sa.CheckConstraint(
            "max_concurrency BETWEEN 1 AND 4",
            name=op.f("ck_external_agent_profile_concurrency_bounds"),
        ),
        sa.CheckConstraint(
            "max_cost_per_task_usd > 0",
            name=op.f("ck_external_agent_profile_task_cost_positive"),
        ),
        sa.CheckConstraint(
            "max_spend_per_day_usd >= max_cost_per_task_usd",
            name=op.f("ck_external_agent_profile_daily_cost_bounds"),
        ),
        sa.PrimaryKeyConstraint("slug", name=op.f("pk_external_agent_profile")),
    )
    op.create_index(
        "ix_external_agent_profile_enabled", "external_agent_profile", ["enabled", "created_at"]
    )
    op.create_table(
        "external_agent_task",
        sa.Column("profile_slug", sa.Text(), nullable=False),
        sa.Column("requested_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("profile_revision", sa.Integer(), nullable=False),
        sa.Column("workload_revision", sa.Integer(), nullable=False),
        sa.Column("reserved_cost_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("external_task_id", sa.Text(), nullable=True),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("reported_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('research', 'summarize', 'classify')",
            name=op.f("ck_external_agent_task_purpose_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'admitted', 'running', 'cancellation_requested', "
            "'succeeded', 'failed', 'expired', 'cancelled', 'outcome_unknown')",
            name=op.f("ck_external_agent_task_status_valid"),
        ),
        sa.CheckConstraint(
            "profile_revision > 0",
            name=op.f("ck_external_agent_task_profile_revision_positive"),
        ),
        sa.CheckConstraint(
            "workload_revision > 0",
            name=op.f("ck_external_agent_task_workload_revision_positive"),
        ),
        sa.CheckConstraint(
            "reserved_cost_usd > 0",
            name=op.f("ck_external_agent_task_reserved_cost_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["profile_slug"], ["external_agent_profile.slug"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["requested_by_operator_id"], ["operator.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_agent_task")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_external_agent_task_idempotency_key")),
        sa.UniqueConstraint(
            "profile_slug",
            "external_task_id",
            name=op.f("uq_external_agent_task_profile_external_task"),
        ),
    )
    op.create_index(
        "ix_external_agent_task_status_created", "external_agent_task", ["status", "created_at"]
    )
    op.create_index(
        "ix_external_agent_task_profile_status",
        "external_agent_task",
        ["profile_slug", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_external_agent_task_profile_status", table_name="external_agent_task")
    op.drop_index("ix_external_agent_task_status_created", table_name="external_agent_task")
    op.drop_table("external_agent_task")
    op.drop_index("ix_external_agent_profile_enabled", table_name="external_agent_profile")
    op.drop_table("external_agent_profile")
    op.execute("DELETE FROM workload_control WHERE name = 'external_agents'")
    op.drop_constraint(op.f("ck_workload_control_name_valid"), "workload_control", type_="check")
    op.create_check_constraint(
        op.f("ck_workload_control_name_valid"),
        "workload_control",
        "name IN ('ai', 'agent_reach', 'processing')",
    )
