"""Operator decisions on domains GDELT discovered (spec B6.7, B11.5)

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    postgresql.ENUM("rejected", "added", name="discovered_domain_status").create(
        op.get_bind(), checkfirst=False
    )
    op.create_table(
        "discovered_domain_decision",
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "rejected", "added", name="discovered_domain_status", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("decided_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["decided_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_discovered_domain_decision_decided_by_operator_id_operator"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
            name=op.f("fk_discovered_domain_decision_source_id_source"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovered_domain_decision")),
        sa.UniqueConstraint("domain", name=op.f("uq_discovered_domain_decision_domain")),
    )


def downgrade() -> None:
    op.drop_table("discovered_domain_decision")
    postgresql.ENUM(name="discovered_domain_status").drop(op.get_bind(), checkfirst=False)
