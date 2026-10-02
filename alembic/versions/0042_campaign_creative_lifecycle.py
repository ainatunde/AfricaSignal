"""Add direct campaign and creative lifecycle records."""

import sqlalchemy as sa

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign",
        sa.Column("sponsor_id", sa.BigInteger(), nullable=False),
        sa.Column("internal_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("currency", sa.Text(), server_default="NGN", nullable=False),
        sa.Column("agreed_fee_minor", sa.BigInteger(), nullable=True),
        sa.Column("agreement_reference", sa.Text(), nullable=True),
        sa.Column("created_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column("approved_by_operator_id", sa.BigInteger(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'active', 'paused', 'ended')",
            name=op.f("ck_campaign_status_valid"),
        ),
        sa.CheckConstraint("revision > 0", name=op.f("ck_campaign_revision_positive")),
        sa.CheckConstraint("currency = 'NGN'", name=op.f("ck_campaign_currency_supported")),
        sa.CheckConstraint(
            "agreed_fee_minor IS NULL OR agreed_fee_minor >= 0",
            name=op.f("ck_campaign_fee_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["sponsor_id"],
            ["sponsor.id"],
            ondelete="RESTRICT",
            name=op.f("fk_campaign_sponsor_id_sponsor"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_campaign_created_by_operator_id_operator"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_campaign_approved_by_operator_id_operator"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign")),
        sa.UniqueConstraint("sponsor_id", "internal_name", name=op.f("uq_campaign_sponsor_id")),
    )
    op.create_index("ix_campaign_status_updated", "campaign", ["status", "updated_at"])
    op.create_table(
        "creative_version",
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("asset_key", sa.Text(), nullable=True),
        sa.Column("alt_text", sa.Text(), nullable=True),
        sa.Column("destination_url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("reviewer_operator_id", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint("version > 0", name=op.f("ck_creative_version_version_positive")),
        sa.CheckConstraint("revision > 0", name=op.f("ck_creative_version_revision_positive")),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'rejected', 'withdrawn')",
            name=op.f("ck_creative_version_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaign.id"],
            ondelete="RESTRICT",
            name=op.f("fk_creative_version_campaign_id_campaign"),
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_operator_id"],
            ["operator.id"],
            name=op.f("fk_creative_version_reviewer_operator_id_operator"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_creative_version_created_by_operator_id_operator"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_creative_version")),
        sa.UniqueConstraint("campaign_id", "version", name=op.f("uq_creative_version_campaign_id")),
    )
    op.create_index(
        "ix_creative_version_campaign_status",
        "creative_version",
        ["campaign_id", "status", "version"],
    )


def downgrade() -> None:
    op.drop_index("ix_creative_version_campaign_status", table_name="creative_version")
    op.drop_table("creative_version")
    op.drop_index("ix_campaign_status_updated", table_name="campaign")
    op.drop_table("campaign")
