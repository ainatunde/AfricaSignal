"""LLM response cache (AS-020)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_response_cache",
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("input_sha256", sa.Text(), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_response_cache")),
        sa.UniqueConstraint(
            "purpose",
            "prompt_version",
            "model_id",
            "input_sha256",
            name=op.f("uq_llm_response_cache_purpose"),
        ),
    )


def downgrade() -> None:
    op.drop_table("llm_response_cache")
