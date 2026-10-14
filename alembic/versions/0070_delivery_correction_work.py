"""Record review work for delivered content affected by later invalidation.

Revision ID: 0070
Revises: 0069
"""

import sqlalchemy as sa

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_correction_work",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("delivery_attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("invalidated_assessment_version_id", sa.BigInteger(), nullable=False),
        sa.Column("reason_key", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("reason_detail", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="pending_review", nullable=False),
        sa.Column("replacement_assessment_version_id", sa.BigInteger()),
        sa.Column("follow_up_delivery_attempt_id", sa.BigInteger()),
        sa.Column("reviewed_by_operator_id", sa.BigInteger()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("operator_note", sa.Text()),
        sa.CheckConstraint(
            "state IN ('pending_review', 'fulfilled', 'blocked', 'dismissed')",
            name="state_valid",
        ),
        sa.CheckConstraint(
            "state != 'fulfilled' OR follow_up_delivery_attempt_id IS NOT NULL",
            name="fulfilled_has_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_attempt_id"], ["delivery_attempt.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["invalidated_assessment_version_id"],
            ["assessment_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["replacement_assessment_version_id"],
            ["assessment_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["follow_up_delivery_attempt_id"], ["delivery_attempt.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_operator_id"], ["operator.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_attempt_id", "reason_key"),
    )
    op.create_index(
        "ix_delivery_correction_work_state_created",
        "delivery_correction_work",
        ["state", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_delivery_correction_work_state_created", table_name="delivery_correction_work"
    )
    op.drop_table("delivery_correction_work")
