"""Add exclusive, revisioned topic sponsorship bookings."""

import sqlalchemy as sa

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "placement_booking",
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("surface", sa.Text(), server_default="explore_topic", nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("creative_version_id", sa.BigInteger(), nullable=False),
        sa.Column("exclusive", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column("approved_by_operator_id", sa.BigInteger(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_by_operator_id", sa.BigInteger(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "surface = 'explore_topic'", name=op.f("ck_placement_booking_surface_allowed")
        ),
        sa.CheckConstraint(
            "topic IN ('energy', 'food')", name=op.f("ck_placement_booking_topic_allowed")
        ),
        sa.CheckConstraint(
            "ends_at > starts_at", name=op.f("ck_placement_booking_interval_positive")
        ),
        sa.CheckConstraint("exclusive IS TRUE", name=op.f("ck_placement_booking_exclusive_only")),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'active', 'paused', 'ended')",
            name=op.f("ck_placement_booking_status_valid"),
        ),
        sa.CheckConstraint("revision > 0", name=op.f("ck_placement_booking_revision_positive")),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaign.id"],
            ondelete="RESTRICT",
            name=op.f("fk_placement_booking_campaign_id_campaign"),
        ),
        sa.ForeignKeyConstraint(
            ["creative_version_id"],
            ["creative_version.id"],
            ondelete="RESTRICT",
            name=op.f("fk_placement_booking_creative_version_id_creative_version"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_placement_booking_created_by_operator_id_operator"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_placement_booking_approved_by_operator_id_operator"),
        ),
        sa.ForeignKeyConstraint(
            ["activated_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_placement_booking_activated_by_operator_id_operator"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_placement_booking")),
    )
    op.create_index(
        "ix_booking_scope_status_interval",
        "placement_booking",
        ["surface", "topic", "status", "starts_at", "ends_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_booking_scope_status_interval", table_name="placement_booking")
    op.drop_table("placement_booking")
