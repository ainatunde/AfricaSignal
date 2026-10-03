"""Shared limits and compact, dedupe-preserving job retention."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_state",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("key_hash", sa.Text(), primary_key=True),
        sa.Column("tokens", sa.Float(), nullable=False, server_default="0"),
        sa.Column("rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("capacity", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "touched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("hits", JSONB(), nullable=False, server_default="[]"),
    )
    op.create_index("ix_rate_limit_state_expires_at", "rate_limit_state", ["expires_at"])
    op.create_table(
        "job_deduplication",
        sa.Column("dedupe_key", sa.Text(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute(
        "INSERT INTO job_deduplication (dedupe_key, created_at) SELECT "
        "dedupe_key, created_at FROM job WHERE dedupe_key IS NOT NULL ON "
        "CONFLICT DO NOTHING"
    )
    op.create_table(
        "job_archive",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), primary_key=True),
        sa.Column("jobs", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_job_finished_retention",
        "job",
        ["finished_at"],
        postgresql_where=sa.text("status IN ('done', 'dead')"),
    )
    # Retain financial records independently of large job payloads.
    op.drop_constraint("fk_llm_call_job_id_job", "llm_call", type_="foreignkey")
    op.create_foreign_key(
        "fk_llm_call_job_id_job", "llm_call", "job", ["job_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_llm_call_job_id_job", "llm_call", type_="foreignkey")
    op.create_foreign_key("fk_llm_call_job_id_job", "llm_call", "job", ["job_id"], ["id"])
    op.drop_index("ix_job_finished_retention", table_name="job")
    op.drop_table("job_archive")
    op.drop_table("job_deduplication")
    op.drop_table("rate_limit_state")
