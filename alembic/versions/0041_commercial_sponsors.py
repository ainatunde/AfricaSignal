"""Add minimal first-party sponsor records."""

import sqlalchemy as sa

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sponsor",
        sa.Column("public_name", sa.Text(), nullable=False),
        sa.Column("website_url", sa.Text(), nullable=False),
        sa.Column("contact_email", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), server_default="unclassified", nullable=False),
        sa.Column("status", sa.Text(), server_default="pending_review", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'paused', 'retired')",
            name=op.f("ck_sponsor_status_valid"),
        ),
        sa.CheckConstraint("revision > 0", name=op.f("ck_sponsor_revision_positive")),
        sa.CheckConstraint(
            "length(public_name) BETWEEN 1 AND 100",
            name=op.f("ck_sponsor_public_name_bounds"),
        ),
        sa.CheckConstraint(
            "length(website_url) BETWEEN 12 AND 500",
            name=op.f("ck_sponsor_website_url_bounds"),
        ),
        sa.CheckConstraint(
            "length(contact_email) BETWEEN 3 AND 254",
            name=op.f("ck_sponsor_contact_email_bounds"),
        ),
        sa.CheckConstraint(
            "category IN ('energy_provider', 'energy_efficiency', 'food_retailer', "
            "'agriculture', 'general_business', 'unclassified')",
            name=op.f("ck_sponsor_category_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_sponsor_created_by_operator_id_operator"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_sponsor_updated_by_operator_id_operator"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sponsor")),
    )
    op.create_index("ix_sponsor_status_created", "sponsor", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_sponsor_status_created", table_name="sponsor")
    op.drop_table("sponsor")
