"""Add durable, audited AI and Agent Reach workload controls.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workload_control",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("schedule", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "name IN ('ai', 'agent_reach', 'processing')",
            name=op.f("ck_workload_control_name_valid"),
        ),
        sa.CheckConstraint("revision > 0", name=op.f("ck_workload_control_revision_positive")),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_workload_control")),
    )
    op.execute(
        """
        INSERT INTO workload_control (name, enabled, schedule, revision) VALUES
          ('ai', true, jsonb_build_object(
              'timezone', 'Africa/Lagos',
              'windows', jsonb_build_array(jsonb_build_object(
                  'days', jsonb_build_array(0, 1, 2, 3, 4, 5, 6),
                  'start', '00:00', 'end', '24:00'
              )),
              'max_concurrency', 4, 'max_items_per_run', 100
          ), 1),
          ('agent_reach', false, jsonb_build_object(
              'timezone', 'Africa/Lagos',
              'windows', jsonb_build_array(jsonb_build_object(
                  'days', jsonb_build_array(0, 1, 2, 3, 4, 5, 6),
                  'start', '01:00', 'end', '04:00'
              )),
              'max_concurrency', 1, 'max_items_per_run', 10
          ), 1),
          ('processing', true, jsonb_build_object(
              'timezone', 'Africa/Lagos',
              'windows', jsonb_build_array(jsonb_build_object(
                  'days', jsonb_build_array(0, 1, 2, 3, 4, 5, 6),
                  'start', '00:00', 'end', '24:00'
              )),
              'max_concurrency', 2, 'max_items_per_run', 50
          ), 1)
        """
    )


def downgrade() -> None:
    op.drop_table("workload_control")
