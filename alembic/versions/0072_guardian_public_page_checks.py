"""Persist bounded visitor-facing Guardian checks.

Revision ID: 0072
Revises: 0071
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "guardian_check_run" in sa.inspect(op.get_bind()).get_table_names():
        return
    observed_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    op.create_table(
        "guardian_check_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("check_code", sa.Text(), nullable=False),
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("observed", observed_type, server_default="{}", nullable=False),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pass', 'fail', 'unknown')", name="status_valid"),
    )
    op.create_index(
        "ix_guardian_check_run_subject_checked",
        "guardian_check_run",
        ["check_code", "subject_key", "checked_at"],
    )
    op.create_index("ix_guardian_check_run_checked", "guardian_check_run", ["checked_at"])


def downgrade() -> None:
    if "guardian_check_run" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_guardian_check_run_checked", table_name="guardian_check_run")
    op.drop_index("ix_guardian_check_run_subject_checked", table_name="guardian_check_run")
    op.drop_table("guardian_check_run")
