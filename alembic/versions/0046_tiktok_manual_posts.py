"""Add TikTok to manual, operator-recorded channel posts.

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-09
"""

import sqlalchemy as sa

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE channel_post_channel ADD VALUE IF NOT EXISTS 'tiktok'")


def downgrade() -> None:
    has_posts = op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM channel_post WHERE channel = 'tiktok')")
    ).scalar_one()
    if has_posts:
        raise RuntimeError("Remove TikTok manual-post records before downgrading migration 0046.")
    op.execute("ALTER TYPE channel_post_channel RENAME TO channel_post_channel_old")
    op.execute("CREATE TYPE channel_post_channel AS ENUM ('wa', 'x')")
    op.execute(
        "ALTER TABLE channel_post ALTER COLUMN channel TYPE channel_post_channel "
        "USING channel::text::channel_post_channel"
    )
    op.execute("DROP TYPE channel_post_channel_old")
