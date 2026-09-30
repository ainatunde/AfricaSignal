"""Byline of an evidence document (AS-027, security review S-08)

Revision ID: 0007
Revises: 0005
Create Date: 2026-09-30
"""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evidence_document", sa.Column("byline", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("evidence_document", "byline")
