"""Add account-bound mentions, reviewed replies, and exact delivery attempt receipts.

Revision ID: 0069
Revises: 0068
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE outbox_status ADD VALUE IF NOT EXISTS 'sending'")
    op.execute("ALTER TYPE outbox_status ADD VALUE IF NOT EXISTS 'outcome_unknown'")
    op.execute("ALTER TYPE outbox_status ADD VALUE IF NOT EXISTS 'simulated'")
    op.add_column("outbox", sa.Column("attempt_started_at", sa.DateTime(timezone=True)))
    op.add_column("outbox", sa.Column("content_sha256", sa.Text()))
    op.add_column("outbox", sa.Column("destination_sha256", sa.Text()))
    op.add_column("outbox", sa.Column("authorization_snapshot", postgresql.JSONB()))
    op.add_column("social_publication", sa.Column("approved_content_sha256", sa.Text()))
    op.add_column("social_publication", sa.Column("destination_account_id", sa.Text()))

    op.create_table(
        "social_mention_poll",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("account_user_id", sa.Text(), nullable=False),
        sa.Column("token_fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "identity_read_performed", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("since_id", sa.Text()),
        sa.Column("newest_id", sa.Text()),
        sa.Column("page_token", sa.Text()),
        sa.Column("next_page_token", sa.Text()),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("reserved_results", sa.Integer(), nullable=False),
        sa.Column("fetched_results", sa.Integer(), server_default="0", nullable=False),
        sa.Column("new_mentions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'outcome_unknown', 'discarded')",
            name="status_valid",
        ),
        sa.CheckConstraint("reserved_results BETWEEN 5 AND 100", name="reserved_results_bounds"),
        sa.CheckConstraint("fetched_results BETWEEN 0 AND 100", name="fetched_results_bounds"),
        sa.CheckConstraint(
            "since_id IS NULL OR since_id ~ '^[0-9]{1,19}$'", name="since_id_numeric"
        ),
        sa.CheckConstraint(
            "newest_id IS NULL OR newest_id ~ '^[0-9]{1,19}$'", name="newest_id_numeric"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_social_mention_poll_status_created",
        "social_mention_poll",
        ["status", "created_at"],
    )
    op.create_table(
        "social_mention",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("poll_id", sa.BigInteger(), nullable=False),
        sa.Column("post_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text()),
        sa.Column("remote_created_at", sa.DateTime(timezone=True)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default="new", nullable=False),
        sa.CheckConstraint("post_id ~ '^[0-9]{1,19}$'", name="post_id_numeric"),
        sa.CheckConstraint(
            "conversation_id IS NULL OR conversation_id ~ '^[0-9]{1,19}$'",
            name="conversation_id_numeric",
        ),
        sa.CheckConstraint(
            "status IN ('new', 'drafted', 'replied', 'escalated', 'dismissed', 'unavailable')",
            name="status_valid",
        ),
        sa.ForeignKeyConstraint(["poll_id"], ["social_mention_poll.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("post_id"),
    )
    op.create_index(
        "ix_social_mention_status_expires", "social_mention", ["status", "expires_at"]
    )
    op.create_table(
        "social_reply",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("mention_id", sa.BigInteger(), nullable=False),
        sa.Column("assessment_version_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by_operator_id", sa.BigInteger(), nullable=False),
        sa.Column("reviewed_by_operator_id", sa.BigInteger()),
        sa.Column("reply_to_post_id", sa.Text(), nullable=False),
        sa.Column("destination_account_id", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("eligibility_decision", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending_review", nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_started_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("external_post_id", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.CheckConstraint(
            "status IN ('pending_review', 'queued', 'sending', 'sent', 'failed', "
            "'outcome_unknown', 'cancelled', 'rejected', 'stale')",
            name="status_valid",
        ),
        sa.CheckConstraint("reply_to_post_id ~ '^[0-9]{1,19}$'", name="reply_to_post_id_numeric"),
        sa.CheckConstraint("content_sha256 ~ '^[a-f0-9]{64}$'", name="content_sha256_valid"),
        sa.ForeignKeyConstraint(["mention_id"], ["social_mention.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["assessment_version_id"], ["assessment_version.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_operator_id"], ["operator.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_operator_id"], ["operator.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mention_id"),
    )
    op.create_index("ix_social_reply_status_created", "social_reply", ["status", "created_at"])
    op.create_table(
        "delivery_attempt",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("outbox_id", sa.BigInteger()),
        sa.Column("social_publication_id", sa.BigInteger()),
        sa.Column("social_reply_id", sa.BigInteger()),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("destination_sha256", sa.Text()),
        sa.Column("destination_account_id", sa.Text()),
        sa.Column("target_reference", sa.Text()),
        sa.Column(
            "assessment_version_ids",
            postgresql.JSONB(),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("authorization_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), server_default="prepared", nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("provider_receipt_id", sa.Text()),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("outcome_detail", sa.Text()),
        sa.CheckConstraint(
            "(outbox_id IS NOT NULL AND social_publication_id IS NULL AND social_reply_id IS NULL) "
            "OR (outbox_id IS NULL AND social_publication_id IS NOT NULL AND social_reply_id IS NULL) "
            "OR (outbox_id IS NULL AND social_publication_id IS NULL AND social_reply_id IS NOT NULL)",
            name="exactly_one_delivery_owner",
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'sent', 'simulated', 'rejected', 'outcome_unknown', 'cancelled')",
            name="status_valid",
        ),
        sa.CheckConstraint("attempt_no > 0", name="attempt_positive"),
        sa.CheckConstraint("content_sha256 ~ '^[a-f0-9]{64}$'", name="content_sha256_valid"),
        sa.ForeignKeyConstraint(["outbox_id"], ["outbox.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["social_publication_id"], ["social_publication.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["social_reply_id"], ["social_reply.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
        sa.UniqueConstraint("outbox_id", "attempt_no"),
        sa.UniqueConstraint("social_publication_id", "attempt_no"),
        sa.UniqueConstraint("social_reply_id", "attempt_no"),
    )
    op.create_index(
        "ix_delivery_attempt_status_created", "delivery_attempt", ["status", "created_at"]
    )
    op.create_index(
        "ix_delivery_attempt_assessment_versions",
        "delivery_attempt",
        ["assessment_version_ids"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_delivery_attempt_assessment_versions", table_name="delivery_attempt"
    )
    op.drop_index("ix_delivery_attempt_status_created", table_name="delivery_attempt")
    op.drop_table("delivery_attempt")
    op.drop_index("ix_social_reply_status_created", table_name="social_reply")
    op.drop_table("social_reply")
    op.drop_index("ix_social_mention_status_expires", table_name="social_mention")
    op.drop_table("social_mention")
    op.drop_index("ix_social_mention_poll_status_created", table_name="social_mention_poll")
    op.drop_table("social_mention_poll")
    op.drop_column("social_publication", "destination_account_id")
    op.drop_column("social_publication", "approved_content_sha256")
    op.drop_column("outbox", "authorization_snapshot")
    op.drop_column("outbox", "destination_sha256")
    op.drop_column("outbox", "content_sha256")
    op.drop_column("outbox", "attempt_started_at")
    op.execute("ALTER TYPE outbox_status RENAME TO outbox_status_old")
    postgresql.ENUM("pending", "sent", "failed", "dead", name="outbox_status").create(
        op.get_bind(), checkfirst=False
    )
    op.execute(
        "ALTER TABLE outbox ALTER COLUMN status TYPE outbox_status "
        "USING (CASE WHEN status IN ('sending', 'outcome_unknown', 'simulated') THEN 'dead' "
        "ELSE status::text END)::outbox_status"
    )
    op.execute("DROP TYPE outbox_status_old")
