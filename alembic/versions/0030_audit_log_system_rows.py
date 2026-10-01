"""Audit rows written by the system, not by an operator (AS-041)

The backup alert job records "alert opened" and "alert resolved" in the audit log. No operator
does that, so ``audit_log.operator_id`` may be empty; such rows are the system's.

Revision ID: 0030
Revises: 0020
Create Date: 2026-09-30
"""

import sqlalchemy as sa

from alembic import op

revision = "0030"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("audit_log", "operator_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    # Dropping the rows would destroy audit history; make the person who downgrades decide.
    remaining = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM audit_log WHERE operator_id IS NULL"))
        .scalar_one()
    )
    if remaining:
        raise RuntimeError(
            f"{remaining} audit_log row(s) were written by the system (operator_id is empty); "
            "export and delete them before downgrading past 0030"
        )
    op.alter_column("audit_log", "operator_id", existing_type=sa.BigInteger(), nullable=False)
