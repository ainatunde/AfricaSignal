"""Add first-party delivery events and rebuildable daily aggregates."""

import sqlalchemy as sa

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_event",
        sa.Column("booking_id", sa.BigInteger(), nullable=False),
        sa.Column("creative_version_id", sa.BigInteger(), nullable=False),
        sa.Column("booking_revision", sa.Integer(), nullable=False),
        sa.Column("event_schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("metric_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deduplication_hash", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validity_status", sa.Text(), server_default="accepted", nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "event_schema_version > 0", name=op.f("ck_delivery_event_event_schema_positive")
        ),
        sa.CheckConstraint(
            "booking_revision > 0", name=op.f("ck_delivery_event_booking_revision_positive")
        ),
        sa.CheckConstraint(
            "metric_version > 0", name=op.f("ck_delivery_event_metric_version_positive")
        ),
        sa.CheckConstraint(
            "metric IN ('eligible_opportunity', 'server_render', 'click')",
            name=op.f("ck_delivery_event_metric_valid"),
        ),
        sa.CheckConstraint(
            "deduplication_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_delivery_event_deduplication_hash_sha256"),
        ),
        sa.CheckConstraint(
            "validity_status IN ('accepted', 'rejected')",
            name=op.f("ck_delivery_event_validity_status_valid"),
        ),
        sa.CheckConstraint(
            "(validity_status = 'accepted' AND rejection_reason IS NULL) OR "
            "(validity_status = 'rejected' AND rejection_reason IS NOT NULL)",
            name=op.f("ck_delivery_event_rejection_reason_matches_status"),
        ),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["placement_booking.id"],
            ondelete="RESTRICT",
            name=op.f("fk_delivery_event_booking_id_placement_booking"),
        ),
        sa.ForeignKeyConstraint(
            ["creative_version_id"],
            ["creative_version.id"],
            ondelete="RESTRICT",
            name=op.f("fk_delivery_event_creative_version_id_creative_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delivery_event")),
        sa.UniqueConstraint(
            "deduplication_hash", name=op.f("uq_delivery_event_deduplication_hash")
        ),
    )
    op.create_index(
        "ix_delivery_event_booking_metric_received",
        "delivery_event",
        ["booking_id", "metric", "received_at"],
    )
    op.create_index("ix_delivery_event_retention", "delivery_event", ["retention_until"])
    op.create_table(
        "delivery_aggregate",
        sa.Column("booking_id", sa.BigInteger(), nullable=False),
        sa.Column("creative_version_id", sa.BigInteger(), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("metric_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("completeness", sa.Text(), server_default="complete", nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "rebuilt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "metric_version > 0", name=op.f("ck_delivery_aggregate_metric_version_positive")
        ),
        sa.CheckConstraint(
            "metric IN ('eligible_opportunity', 'server_render', 'click')",
            name=op.f("ck_delivery_aggregate_metric_valid"),
        ),
        sa.CheckConstraint("count >= 0", name=op.f("ck_delivery_aggregate_count_nonnegative")),
        sa.CheckConstraint(
            "completeness IN ('complete', 'partial', 'unavailable')",
            name=op.f("ck_delivery_aggregate_completeness_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["placement_booking.id"],
            ondelete="RESTRICT",
            name=op.f("fk_delivery_aggregate_booking_id_placement_booking"),
        ),
        sa.ForeignKeyConstraint(
            ["creative_version_id"],
            ["creative_version.id"],
            ondelete="RESTRICT",
            name=op.f("fk_delivery_aggregate_creative_version_id_creative_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delivery_aggregate")),
        sa.UniqueConstraint(
            "booking_id",
            "creative_version_id",
            "metric",
            "metric_version",
            "period_start",
            name=op.f("uq_delivery_aggregate_booking_id"),
        ),
    )
    op.create_index(
        "ix_delivery_aggregate_period", "delivery_aggregate", ["period_start", "metric"]
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_aggregate_period", table_name="delivery_aggregate")
    op.drop_table("delivery_aggregate")
    op.drop_index("ix_delivery_event_retention", table_name="delivery_event")
    op.drop_index("ix_delivery_event_booking_metric_received", table_name="delivery_event")
    op.drop_table("delivery_event")
