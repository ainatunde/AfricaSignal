"""Add revision-bound commercial package drafts."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commercial_draft",
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="package", nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("input_refs", JSONB(), nullable=False),
        sa.Column("facts", JSONB(), nullable=False),
        sa.Column("reason_codes", JSONB(), nullable=False),
        sa.Column("created_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column("reviewed_by_operator_id", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint("kind = 'package'", name=op.f("ck_commercial_draft_kind_valid")),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'rejected')",
            name=op.f("ck_commercial_draft_status_valid"),
        ),
        sa.CheckConstraint("revision > 0", name=op.f("ck_commercial_draft_revision_positive")),
        sa.CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_commercial_draft_input_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaign.id"],
            ondelete="RESTRICT",
            name=op.f("fk_commercial_draft_campaign_id_campaign"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_commercial_draft_created_by_operator_id_operator"),
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_commercial_draft_reviewed_by_operator_id_operator"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commercial_draft")),
        sa.UniqueConstraint("kind", "input_hash", name=op.f("uq_commercial_draft_kind_input_hash")),
    )
    op.create_index(
        "ix_commercial_draft_status_created", "commercial_draft", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_commercial_draft_status_created", table_name="commercial_draft")
    op.drop_table("commercial_draft")
