"""Ledger of deleted accounts, for re-applying deletions after a backup restore (AS-043 gap G4)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-30
"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_deletion",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("email_hmac", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mirrored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_deletion")),
    )
    op.create_index(
        op.f("ix_account_deletion_email_hmac"), "account_deletion", ["email_hmac"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_account_deletion_email_hmac"), table_name="account_deletion")
    op.drop_table("account_deletion")
