"""When a reader session was last used, for the idle limit (security review S-17)

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-01
"""

import sqlalchemy as sa

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("session", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("session", "last_seen_at")
