"""Persist bounded Guardian collection repair attempts."""

import sqlalchemy as sa

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guardian_repair_attempt",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("incident_key", sa.Text(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("failed_job_id", sa.BigInteger(), nullable=False),
        sa.Column("retry_job_id", sa.BigInteger(), nullable=True),
        sa.Column("source_generation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_class", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="queued", nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('queued', 'recovered', 'completed_without_recovery', 'exhausted')",
            name="state_valid",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_key"),
    )
    op.create_index(
        "ix_guardian_repair_state_created",
        "guardian_repair_attempt",
        ["state", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_guardian_repair_state_created", table_name="guardian_repair_attempt")
    op.drop_table("guardian_repair_attempt")
