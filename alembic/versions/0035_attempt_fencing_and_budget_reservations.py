"""Fence reclaimed jobs and reserve model spend before provider calls.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job", sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute(
        """
        WITH legacy_claims AS (
            SELECT id, gen_random_uuid() AS token FROM job WHERE status = 'running'
        )
        UPDATE job
        SET lease_token = legacy_claims.token,
            locked_by = 'legacy:' || legacy_claims.token::text
        FROM legacy_claims
        WHERE job.id = legacy_claims.id
        """
    )
    op.create_table(
        "llm_budget_reservation",
        sa.Column("budget_day", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("input_token_bound", sa.Integer(), nullable=False),
        sa.Column("output_token_bound", sa.Integer(), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(10, 5), nullable=False),
        sa.Column("actual_cost_usd", sa.Numeric(10, 5), nullable=True),
        sa.Column("state", sa.Text(), server_default="reserved", nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "state IN ('reserved', 'uncertain', 'settled')",
            name=op.f("ck_llm_budget_reservation_state_valid"),
        ),
        sa.CheckConstraint(
            "reserved_usd >= 0", name=op.f("ck_llm_budget_reservation_amount_nonnegative")
        ),
        sa.CheckConstraint(
            "input_token_bound >= 0", name=op.f("ck_llm_budget_reservation_input_nonnegative")
        ),
        sa.CheckConstraint(
            "output_token_bound >= 0", name=op.f("ck_llm_budget_reservation_output_nonnegative")
        ),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_budget_reservation")),
    )
    op.create_index(
        "ix_llm_budget_reservation_day_state",
        "llm_budget_reservation",
        ["budget_day", "state"],
    )
    op.create_index(
        "ix_llm_budget_reservation_job_state",
        "llm_budget_reservation",
        ["job_id", "state"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_budget_reservation_job_state", table_name="llm_budget_reservation")
    op.drop_index("ix_llm_budget_reservation_day_state", table_name="llm_budget_reservation")
    op.drop_table("llm_budget_reservation")
    op.drop_column("job", "lease_token")
