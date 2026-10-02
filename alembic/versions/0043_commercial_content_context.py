"""Add a revision-bound commercial content context projection."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_context",
        sa.Column("assessment_version_id", sa.BigInteger(), nullable=False),
        sa.Column("canonical_place_id", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=False),
        sa.Column("classifier_version", sa.Text(), nullable=False),
        sa.Column("topic_tags", JSONB(), nullable=False),
        sa.Column("evidence_refs", JSONB(), nullable=False),
        sa.Column("source_refs", JSONB(), nullable=False),
        sa.Column("reason_codes", JSONB(), nullable=False),
        sa.Column("suitability", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_content_context_content_hash_sha256")
        ),
        sa.CheckConstraint("revision > 0", name=op.f("ck_content_context_revision_positive")),
        sa.CheckConstraint(
            "length(taxonomy_version) BETWEEN 1 AND 80",
            name=op.f("ck_content_context_taxonomy_version_bounds"),
        ),
        sa.CheckConstraint(
            "length(classifier_version) BETWEEN 1 AND 80",
            name=op.f("ck_content_context_classifier_version_bounds"),
        ),
        sa.CheckConstraint(
            "suitability IN ('eligible', 'restricted', 'unknown', 'invalidated')",
            name=op.f("ck_content_context_suitability_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["assessment_version_id"],
            ["assessment_version.id"],
            ondelete="RESTRICT",
            name=op.f("fk_content_context_assessment_version_id_assessment_version"),
        ),
        sa.ForeignKeyConstraint(
            ["canonical_place_id"],
            ["place.id"],
            ondelete="RESTRICT",
            name=op.f("fk_content_context_canonical_place_id_place"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_context")),
        sa.UniqueConstraint(
            "assessment_version_id",
            "content_hash",
            "taxonomy_version",
            "classifier_version",
            "revision",
            name=op.f("uq_content_context_assessment_version_id"),
        ),
    )
    op.create_index(
        "ix_content_context_assessment_suitability",
        "content_context",
        ["assessment_version_id", "suitability"],
    )
    op.create_index("ix_content_context_expiry", "content_context", ["expires_at"])
    op.create_index(
        "ix_content_context_source_refs",
        "content_context",
        ["source_refs"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_content_context_source_refs", table_name="content_context")
    op.drop_index("ix_content_context_expiry", table_name="content_context")
    op.drop_index("ix_content_context_assessment_suitability", table_name="content_context")
    op.drop_table("content_context")
