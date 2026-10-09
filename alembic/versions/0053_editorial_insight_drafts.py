"""Add private operator-reviewed editorial insight drafts.

Revision ID: 0053
Revises: 0052
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editorial_insight_draft",
        sa.Column("assessment_version_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending_review", nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("input_sha256", sa.Text(), nullable=False),
        sa.Column("evidence_claim_ids", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("reviewed_by_operator_id", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'stale')",
            name=op.f("ck_editorial_insight_draft_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["assessment_version_id"],
            ["assessment_version.id"],
            ondelete="RESTRICT",
            name=op.f("fk_editorial_insight_draft_assessment_version_id_assessment_version"),
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_editorial_insight_draft_reviewed_by_operator_id_operator"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_editorial_insight_draft")),
        sa.UniqueConstraint(
            "assessment_version_id",
            "prompt_version",
            name=op.f("uq_editorial_insight_draft_assessment_version_id"),
        ),
    )
    op.create_index(
        "ix_editorial_insight_draft_status_created",
        "editorial_insight_draft",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_editorial_insight_draft_status_created", table_name="editorial_insight_draft")
    op.drop_table("editorial_insight_draft")
