"""Persist publication eligibility provenance."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assessment_version", sa.Column("publication_eligibility", JSONB(), nullable=True)
    )
    op.add_column("social_publication", sa.Column("eligibility_decision", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("social_publication", "eligibility_decision")
    op.drop_column("assessment_version", "publication_eligibility")
