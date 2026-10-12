"""Add durable, operator-approved social publication attempts.

Revision ID: 0050
Revises: 0039
Create Date: 2026-10-09
"""

import sqlalchemy as sa

from alembic import op

revision = "0050"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "social_publication",
        sa.Column("assessment_version_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), server_default="x", nullable=False),
        sa.Column("requested_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "approved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("external_post_id", sa.Text(), nullable=True),
        sa.Column("attempt_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint("channel = 'x'", name=op.f("ck_social_publication_channel_valid")),
        sa.CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed', 'outcome_unknown', 'cancelled')",
            name=op.f("ck_social_publication_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["assessment_version_id"],
            ["assessment_version.id"],
            ondelete="RESTRICT",
            name=op.f("fk_social_publication_assessment_version_id_assessment_version"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_operator_id"],
            ["operator.id"],
            ondelete="RESTRICT",
            name=op.f("fk_social_publication_requested_by_operator_id_operator"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_publication")),
        sa.UniqueConstraint(
            "assessment_version_id",
            "channel",
            name=op.f("uq_social_publication_assessment_version_id"),
        ),
    )
    op.create_index(
        "ix_social_publication_status_created",
        "social_publication",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_social_publication_status_created", table_name="social_publication")
    op.drop_table("social_publication")
