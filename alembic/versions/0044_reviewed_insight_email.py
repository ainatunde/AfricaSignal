"""Add opt-in email delivery for reviewed editorial insights.

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-09
"""

import sqlalchemy as sa

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def _replace_outbox_kind(values: tuple[str, ...]) -> None:
    """Replace the enum atomically; PostgreSQL has no DROP VALUE operation."""
    op.execute("ALTER TYPE outbox_kind RENAME TO outbox_kind_old")
    literals = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    op.execute(f"CREATE TYPE outbox_kind AS ENUM ({literals})")
    op.execute(
        "ALTER TABLE outbox ALTER COLUMN kind TYPE outbox_kind USING kind::text::outbox_kind"
    )
    op.execute("DROP TYPE outbox_kind_old")


def upgrade() -> None:
    _replace_outbox_kind(("email_login", "email_digest", "email_correction", "email_insight"))
    op.add_column(
        "app_user",
        sa.Column("insight_email_opt_in", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("app_user", sa.Column("insight_email_opt_in_at", sa.DateTime(timezone=True)))
    op.drop_constraint(
        op.f("ck_editorial_insight_draft_status_valid"),
        "editorial_insight_draft",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_editorial_insight_draft_status_valid"),
        "editorial_insight_draft",
        "status IN ('pending_review', 'approved', 'email_queued', 'rejected', 'stale')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM outbox WHERE kind = 'email_insight'")
    op.drop_constraint(
        op.f("ck_editorial_insight_draft_status_valid"),
        "editorial_insight_draft",
        type_="check",
    )
    op.execute(
        "UPDATE editorial_insight_draft SET status = 'approved' WHERE status = 'email_queued'"
    )
    op.create_check_constraint(
        op.f("ck_editorial_insight_draft_status_valid"),
        "editorial_insight_draft",
        "status IN ('pending_review', 'approved', 'rejected', 'stale')",
    )
    op.drop_column("app_user", "insight_email_opt_in_at")
    op.drop_column("app_user", "insight_email_opt_in")
    _replace_outbox_kind(("email_login", "email_digest", "email_correction"))
