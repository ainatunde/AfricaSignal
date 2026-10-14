"""Persist deterministic multi-assessment insight candidates."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "insight_candidate",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("candidate_type", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending_review", nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("packet", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stale_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_by_operator_id", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "candidate_type IN ('material_change', 'persistent_trend', 'multi_series_comparison', "
            "'evidence_disagreement')",
            name="candidate_type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending_review', 'selected', 'rejected', 'stale')",
            name="status_valid",
        ),
        sa.ForeignKeyConstraint(["reviewed_by_operator_id"], ["operator.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_insight_candidate_status_created",
        "insight_candidate",
        ["status", "created_at"],
    )
    op.create_table(
        "insight_candidate_assessment",
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("assessment_version_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["insight_candidate.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["assessment_version_id"], ["assessment_version.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "role IN ('current', 'prior', 'series_1', 'series_2')", name="role_valid"
        ),
        sa.PrimaryKeyConstraint("candidate_id", "assessment_version_id"),
    )
    op.create_index(
        "ix_insight_candidate_assessment_version",
        "insight_candidate_assessment",
        ["assessment_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_insight_candidate_assessment_version", table_name="insight_candidate_assessment"
    )
    op.drop_table("insight_candidate_assessment")
    op.drop_index("ix_insight_candidate_status_created", table_name="insight_candidate")
    op.drop_table("insight_candidate")
