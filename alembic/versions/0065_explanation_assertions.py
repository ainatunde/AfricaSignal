"""Persist provenance for each generated explanation assertion."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assessment_version", sa.Column("explanation_assertions", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("assessment_version", "explanation_assertions")
