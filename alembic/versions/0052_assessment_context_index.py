"""Index assessment-linked evidence retrieval.

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-09
"""

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_assessment_input_version_kind_id",
        "assessment_input",
        ["assessment_version_id", "input_kind", "input_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_assessment_input_version_kind_id", table_name="assessment_input")
