"""Record that an operator posted a channel draft by hand (AS-033)

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    postgresql.ENUM("wa", "x", name="channel_post_channel").create(op.get_bind(), checkfirst=False)
    op.create_table(
        "channel_post",
        sa.Column("assessment_version_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "channel",
            postgresql.ENUM("wa", "x", name="channel_post_channel", create_type=False),
            nullable=False,
        ),
        sa.Column("posted_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "posted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("post_url", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["assessment_version_id"],
            ["assessment_version.id"],
            name=op.f("fk_channel_post_assessment_version_id_assessment_version"),
        ),
        sa.ForeignKeyConstraint(
            ["posted_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_channel_post_posted_by_operator_id_operator"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_post")),
        sa.UniqueConstraint(
            "assessment_version_id", "channel", name=op.f("uq_channel_post_assessment_version_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("channel_post")
    postgresql.ENUM(name="channel_post_channel").drop(op.get_bind(), checkfirst=False)
