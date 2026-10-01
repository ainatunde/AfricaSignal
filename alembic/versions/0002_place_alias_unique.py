"""One alias per place and normalised text

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_place_alias_place_id", "place_alias", ["place_id", "alias_norm"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_place_alias_place_id", "place_alias", type_="unique")
