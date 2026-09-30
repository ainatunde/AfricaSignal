"""Hold time for first high-severity assessments (publication rule R7, AS-012)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assessment_version", sa.Column("hold_until", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("assessment_version", "hold_until")
