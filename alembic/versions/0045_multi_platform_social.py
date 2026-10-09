"""Add platform targets and optional media to social publication records.

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-09
"""

import sqlalchemy as sa

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_social_publication_channel_valid"), "social_publication", type_="check")
    op.create_check_constraint(
        op.f("ck_social_publication_channel_valid"),
        "social_publication",
        "channel IN ('x', 'facebook', 'instagram', 'telegram', 'youtube')",
    )
    op.add_column("social_publication", sa.Column("media_storage_key", sa.Text(), nullable=True))
    op.add_column("social_publication", sa.Column("media_content_type", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM social_publication WHERE channel <> 'x'")
    op.drop_column("social_publication", "media_content_type")
    op.drop_column("social_publication", "media_storage_key")
    op.drop_constraint(op.f("ck_social_publication_channel_valid"), "social_publication", type_="check")
    op.create_check_constraint(
        op.f("ck_social_publication_channel_valid"), "social_publication", "channel = 'x'"
    )
