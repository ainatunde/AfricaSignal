"""Mark console-owned source pacing overrides so source imports preserve them.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-02
"""

import sqlalchemy as sa

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "source",
        sa.Column("pacing_override", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("source", "pacing_override")
