"""Durable approved-source research lineage; no capability is enabled by migration.

Revision ID: 0073
Revises: 0072
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def _base():
    return [
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def _ref(name, table, nullable=False, unique=False):
    return sa.Column(
        name, sa.BigInteger(), sa.ForeignKey(f"{table}.id"), nullable=nullable, unique=unique
    )


def upgrade() -> None:
    op.create_table(
        "research_question",
        *_base(),
        sa.Column("identity", sa.Text(), nullable=False, unique=True),
        _ref("situation_id", "situation"),
        _ref("version_id", "assessment_version"),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("generation", sa.Text(), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("scope", JSONB(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_count", sa.Integer(), nullable=False),
        sa.Column("model_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("fetch_count >= 0 AND model_count >= 0", name="counters_nonnegative"),
        sa.CheckConstraint(
            "state IN ('open','queued','researching','evidence_ready','validated',"
            "'applying','verifying','resolved','waiting_permission','waiting_budget',"
            "'waiting_window','unresolved','escalated','cancelled')",
            name="state_valid",
        ),
    )
    op.create_index("ix_research_question_state_due", "research_question", ["state", "next_due_at"])
    op.create_table(
        "research_attempt",
        *_base(),
        _ref("question_id", "research_question"),
        _ref("source_id", "source"),
        sa.Column("permission_version", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        _ref("evidence_id", "evidence_document", nullable=True),
        sa.Column("detail", JSONB(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('reserved','completed','captured','failed','permission_changed')",
            name="outcome_valid",
        ),
    )
    op.create_index(
        "ix_research_attempt_question", "research_attempt", ["question_id", "created_at"]
    )
    op.create_table(
        "research_decision",
        *_base(),
        _ref("question_id", "research_question", unique=True),
        _ref("evidence_id", "evidence_document"),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("provenance", JSONB(), nullable=False),
        sa.CheckConstraint(
            "verdict IN ('supported','contradicted','insufficient','out_of_scope')",
            name="verdict_valid",
        ),
    )
    op.create_table(
        "correction_action",
        *_base(),
        _ref("question_id", "research_question", unique=True),
        _ref("decision_id", "research_decision", unique=True),
        _ref("before_version_id", "assessment_version"),
        _ref("after_version_id", "assessment_version", nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('queued','applying','verifying','verified','blocked')",
            name="state_valid",
        ),
    )
    op.create_index("ix_correction_action_state", "correction_action", ["state", "created_at"])


def downgrade() -> None:
    for table in (
        "correction_action",
        "research_decision",
        "research_attempt",
        "research_question",
    ):
        op.drop_table(table)
