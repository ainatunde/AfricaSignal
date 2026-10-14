"""Add an inactive bounded X reply policy and usefulness labels for insight reviews.

Revision ID: 0071
Revises: 0070
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("insight_candidate", sa.Column("usefulness_rating", sa.Integer()))
    op.add_column("insight_candidate", sa.Column("scope_correct", sa.Boolean()))
    op.add_column("insight_candidate", sa.Column("unsupported_critical", sa.Boolean()))
    op.create_check_constraint(
        "usefulness_rating_bounds",
        "insight_candidate",
        "usefulness_rating IS NULL OR usefulness_rating BETWEEN 1 AND 5",
    )

    op.add_column("social_mention", sa.Column("triage_category", sa.Text()))
    op.create_check_constraint(
        "triage_category_valid",
        "social_mention",
        "triage_category IS NULL OR triage_category IN "
        "('dated_fact', 'source_request', 'clarification', 'correction_report', "
        "'unsupported_premise', 'out_of_scope', 'abuse_or_spam', 'other')",
    )

    op.create_table(
        "social_reply_policy",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("account_user_id", sa.Text()),
        sa.Column("evaluation_case_set", sa.Text()),
        sa.Column("evaluation_evidence_ref", sa.Text()),
        sa.Column("evaluation_case_count", sa.Integer()),
        sa.Column("evaluation_coverage", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("evaluation_critical_errors", sa.Integer()),
        sa.Column("evaluated_by_operator_id", sa.BigInteger()),
        sa.Column("evaluated_at", sa.DateTime(timezone=True)),
        sa.Column("approved_by_operator_id", sa.BigInteger()),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("approval_reason", sa.Text()),
        sa.Column("staging_evidence_ref", sa.Text()),
        sa.Column("activated_by_operator_id", sa.BigInteger()),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("activation_reason", sa.Text()),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        sa.CheckConstraint(
            "status IN ('draft', 'evaluated', 'approved', 'active', 'retired')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "account_user_id IS NULL OR account_user_id ~ '^[0-9]{1,19}$'",
            name="account_user_id_numeric",
        ),
        sa.CheckConstraint(
            "evaluation_case_count IS NULL OR evaluation_case_count >= 0",
            name="evaluation_case_count_nonnegative",
        ),
        sa.CheckConstraint(
            "evaluation_critical_errors IS NULL OR evaluation_critical_errors >= 0",
            name="evaluation_critical_errors_nonnegative",
        ),
        sa.CheckConstraint(
            "status = 'draft' OR "
            "(evaluation_case_set IS NOT NULL AND evaluation_evidence_ref IS NOT NULL "
            "AND evaluation_case_count > 0 "
            "AND evaluation_coverage IS NOT NULL AND evaluation_critical_errors = 0 "
            "AND evaluated_by_operator_id IS NOT NULL AND evaluated_at IS NOT NULL)",
            name="review_states_require_evaluation",
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'active', 'retired') OR "
            "(approved_by_operator_id IS NOT NULL AND approved_at IS NOT NULL "
            "AND approval_reason IS NOT NULL)",
            name="approved_states_require_approval",
        ),
        sa.CheckConstraint(
            "status != 'active' OR "
            "(staging_evidence_ref IS NOT NULL AND activated_by_operator_id IS NOT NULL "
            "AND activated_at IS NOT NULL AND activation_reason IS NOT NULL)",
            name="active_requires_staging_and_activation",
        ),
        sa.ForeignKeyConstraint(["evaluated_by_operator_id"], ["operator.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approved_by_operator_id"], ["operator.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["activated_by_operator_id"], ["operator.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision"),
    )
    op.create_index(
        "uq_social_reply_policy_active",
        "social_reply_policy",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    policy_table = sa.table(
        "social_reply_policy",
        sa.column("revision", sa.Integer()),
        sa.column("status", sa.Text()),
        sa.column("policy", postgresql.JSONB(astext_type=sa.Text())),
    )
    op.bulk_insert(
        policy_table,
        [
            {
                "revision": 1,
                "status": "draft",
                "policy": {
                    "channel": "x",
                    "country_code": "NG",
                    "allowed_topics": ["energy", "food"],
                    "allowed_categories": ["dated_fact", "source_request"],
                    "escalate_categories": [
                        "abuse_or_spam",
                        "clarification",
                        "correction_report",
                        "other",
                        "out_of_scope",
                        "unsupported_premise",
                    ],
                    "max_replies_per_lagos_day": 10,
                    "max_replies_per_conversation": 1,
                    "requires_operator_triage": True,
                    "requires_assessment_selection": True,
                    "requires_individual_reply_review": True,
                    "stores_mention_text": False,
                },
            }
        ],
    )

    op.add_column("social_reply", sa.Column("policy_revision", sa.Integer()))
    op.add_column("social_reply", sa.Column("authorized_at", sa.DateTime(timezone=True)))
    op.create_foreign_key(
        "fk_social_reply_policy_revision",
        "social_reply",
        "social_reply_policy",
        ["policy_revision"],
        ["revision"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_social_reply_policy_revision", "social_reply", type_="foreignkey")
    op.drop_column("social_reply", "authorized_at")
    op.drop_column("social_reply", "policy_revision")
    op.drop_index("uq_social_reply_policy_active", table_name="social_reply_policy")
    op.drop_table("social_reply_policy")
    op.drop_constraint("triage_category_valid", "social_mention", type_="check")
    op.drop_column("social_mention", "triage_category")
    op.drop_constraint("usefulness_rating_bounds", "insight_candidate", type_="check")
    op.drop_column("insight_candidate", "unsupported_critical")
    op.drop_column("insight_candidate", "scope_correct")
    op.drop_column("insight_candidate", "usefulness_rating")
