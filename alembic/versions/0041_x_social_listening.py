"""Add budgeted X Recent Search watch queries and ID-only leads.

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-09
"""

import sqlalchemy as sa

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "social_listening_query",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("max_results", sa.Integer(), server_default="20", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_seen_post_id", sa.Text(), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "max_results BETWEEN 10 AND 100",
            name=op.f("ck_social_listening_query_max_results_bounds"),
        ),
        sa.CheckConstraint(
            "revision > 0", name=op.f("ck_social_listening_query_revision_positive")
        ),
        sa.CheckConstraint(
            "last_seen_post_id IS NULL OR last_seen_post_id ~ '^[0-9]+$'",
            name=op.f("ck_social_listening_query_last_seen_post_id_numeric"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_listening_query")),
        sa.UniqueConstraint("name", name=op.f("uq_social_listening_query_name")),
    )
    op.create_table(
        "social_listening_poll",
        sa.Column("query_id", sa.BigInteger(), nullable=False),
        sa.Column("query_revision", sa.Integer(), nullable=False),
        sa.Column("started_by_operator_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("reserved_results", sa.Integer(), nullable=False),
        sa.Column("fetched_results", sa.Integer(), server_default="0", nullable=False),
        sa.Column("new_leads", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'outcome_unknown', 'discarded')",
            name=op.f("ck_social_listening_poll_status_valid"),
        ),
        sa.CheckConstraint(
            "reserved_results BETWEEN 10 AND 100",
            name=op.f("ck_social_listening_poll_reserved_results_bounds"),
        ),
        sa.CheckConstraint(
            "fetched_results BETWEEN 0 AND 100",
            name=op.f("ck_social_listening_poll_fetched_results_bounds"),
        ),
        sa.ForeignKeyConstraint(
            ["query_id"],
            ["social_listening_query.id"],
            ondelete="RESTRICT",
            name=op.f("fk_social_listening_poll_query_id_social_listening_query"),
        ),
        sa.ForeignKeyConstraint(
            ["started_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_social_listening_poll_started_by_operator_id_operator"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_listening_poll")),
    )
    op.create_index(
        "ix_social_listening_poll_status_created", "social_listening_poll", ["status", "created_at"]
    )
    op.create_index(
        "ix_social_listening_poll_query_created",
        "social_listening_poll",
        ["query_id", "created_at"],
    )
    op.create_table(
        "social_listening_lead",
        sa.Column("query_id", sa.BigInteger(), nullable=False),
        sa.Column("poll_id", sa.BigInteger(), nullable=False),
        sa.Column("post_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="new", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "status IN ('new', 'reviewed', 'dismissed', 'unavailable')",
            name=op.f("ck_social_listening_lead_status_valid"),
        ),
        sa.CheckConstraint(
            "post_id ~ '^[0-9]+$'", name=op.f("ck_social_listening_lead_post_id_numeric")
        ),
        sa.ForeignKeyConstraint(
            ["query_id"],
            ["social_listening_query.id"],
            ondelete="RESTRICT",
            name=op.f("fk_social_listening_lead_query_id_social_listening_query"),
        ),
        sa.ForeignKeyConstraint(
            ["poll_id"],
            ["social_listening_poll.id"],
            ondelete="RESTRICT",
            name=op.f("fk_social_listening_lead_poll_id_social_listening_poll"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_listening_lead")),
        sa.UniqueConstraint("post_id", name=op.f("uq_social_listening_lead_post_id")),
    )
    op.create_index(
        "ix_social_listening_lead_status_expires", "social_listening_lead", ["status", "expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_social_listening_lead_status_expires", table_name="social_listening_lead")
    op.drop_table("social_listening_lead")
    op.drop_index("ix_social_listening_poll_query_created", table_name="social_listening_poll")
    op.drop_index("ix_social_listening_poll_status_created", table_name="social_listening_poll")
    op.drop_table("social_listening_poll")
    op.drop_table("social_listening_query")
