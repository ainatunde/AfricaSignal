"""Range-validation queue for measurements (spec B6.3, AS-010)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    postgresql.ENUM("pending", "approved", "rejected", name="measurement_review_status").create(
        op.get_bind(), checkfirst=False
    )
    op.create_table(
        "measurement_review",
        sa.Column("series_id", sa.BigInteger(), nullable=False),
        sa.Column("place_id", sa.BigInteger(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("vintage", sa.Date(), nullable=False),
        sa.Column("evidence_document_id", sa.BigInteger(), nullable=False),
        sa.Column("reference_value", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "approved",
                "rejected",
                name="measurement_review_status",
                create_type=False,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_document_id"],
            ["evidence_document.id"],
            name=op.f("fk_measurement_review_evidence_document_id_evidence_document"),
        ),
        sa.ForeignKeyConstraint(
            ["place_id"], ["place.id"], name=op.f("fk_measurement_review_place_id_place")
        ),
        sa.ForeignKeyConstraint(
            ["series_id"], ["series.id"], name=op.f("fk_measurement_review_series_id_series")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_measurement_review")),
        sa.UniqueConstraint(
            "series_id",
            "place_id",
            "period_start",
            "vintage",
            name=op.f("uq_measurement_review_series_id"),
        ),
    )


def downgrade() -> None:
    op.drop_table("measurement_review")
    postgresql.ENUM(name="measurement_review_status").drop(op.get_bind(), checkfirst=False)
