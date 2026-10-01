"""Policy series added by operators in the console (spec B8.1)

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    postgresql.ENUM("national", "states", name="policy_scope").create(
        op.get_bind(), checkfirst=False
    )
    op.create_table(
        "operator_policy_series",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "topic",
            postgresql.ENUM("energy", "food", name="topic", create_type=False),
            nullable=False,
        ),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("primary_sources", postgresql.JSONB(), nullable=False),
        sa.Column(
            "scope",
            postgresql.ENUM("national", "states", name="policy_scope", create_type=False),
            nullable=False,
        ),
        sa.Column("state_codes", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("affected_groups", sa.Text(), nullable=False),
        sa.Column("materiality_pct", sa.Numeric(6, 2), server_default="5.0", nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_operator_policy_series_created_by_operator_id_operator"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operator_policy_series")),
        sa.UniqueConstraint("code", name=op.f("uq_operator_policy_series_code")),
    )


def downgrade() -> None:
    op.drop_table("operator_policy_series")
    postgresql.ENUM(name="policy_scope").drop(op.get_bind(), checkfirst=False)
