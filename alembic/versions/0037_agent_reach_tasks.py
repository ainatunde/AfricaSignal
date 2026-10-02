"""Add bounded Agent Reach tasks and candidate metadata.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-02
"""

import sqlalchemy as sa

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_reach_task",
        sa.Column("requested_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("max_results", sa.Integer(), nullable=False),
        sa.Column("control_revision", sa.Integer(), nullable=False),
        sa.Column("runner_endpoint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("external_task_id", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "topic IN ('energy', 'food')", name=op.f("ck_agent_reach_task_topic_valid")
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancellation_requested', "
            "'cancelled', 'expired', 'outcome_unknown')",
            name=op.f("ck_agent_reach_task_status_valid"),
        ),
        sa.CheckConstraint(
            "max_results BETWEEN 1 AND 20", name=op.f("ck_agent_reach_task_max_results_bounds")
        ),
        sa.CheckConstraint(
            "control_revision > 0", name=op.f("ck_agent_reach_task_control_revision_positive")
        ),
        sa.ForeignKeyConstraint(["requested_by_operator_id"], ["operator.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_reach_task")),
        sa.UniqueConstraint("external_task_id", name=op.f("uq_agent_reach_task_external_task_id")),
    )
    op.create_index(
        "ix_agent_reach_task_status_created",
        "agent_reach_task",
        ["status", "created_at"],
    )
    op.create_table(
        "agent_reach_candidate",
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("publisher", sa.Text(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("queued_source_id", sa.BigInteger(), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'rejected', 'fetch_queued')",
            name=op.f("ck_agent_reach_candidate_status_valid"),
        ),
        sa.ForeignKeyConstraint(["task_id"], ["agent_reach_task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["queued_source_id"], ["source.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_reach_candidate")),
        sa.UniqueConstraint(
            "task_id", "canonical_url", name=op.f("uq_agent_reach_candidate_task_canonical_url")
        ),
    )
    op.create_index(
        "ix_agent_reach_candidate_status_created",
        "agent_reach_candidate",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_reach_candidate_status_created", table_name="agent_reach_candidate")
    op.drop_table("agent_reach_candidate")
    op.drop_index("ix_agent_reach_task_status_created", table_name="agent_reach_task")
    op.drop_table("agent_reach_task")
