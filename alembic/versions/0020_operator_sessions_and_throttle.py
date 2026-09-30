"""Console session revocation and database-backed sign-in throttle (AS-042, S-04 and S-05)

Revision ID: 0020
Revises: 0005
Create Date: 2026-09-30

``down_revision`` is 0005, the head of claude/integration-1 when this was written; the integration
branch re-chains it if other migrations land first.
"""

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "operator",
        sa.Column("session_epoch", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("operator", sa.Column("last_totp_step", sa.BigInteger(), nullable=True))
    op.create_table(
        "operator_sign_in_failure",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("client_key", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operator_sign_in_failure")),
    )
    op.create_index(
        "ix_operator_sign_in_failure_email_at", "operator_sign_in_failure", ["email", "at"]
    )


def downgrade() -> None:
    op.drop_index("ix_operator_sign_in_failure_email_at", table_name="operator_sign_in_failure")
    op.drop_table("operator_sign_in_failure")
    op.drop_column("operator", "last_totp_step")
    op.drop_column("operator", "session_epoch")
