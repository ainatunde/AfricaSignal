"""Add default-off commercial controls and workload admission."""

import sqlalchemy as sa

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_workload_control_name_valid"), "workload_control", type_="check")
    op.create_check_constraint(
        op.f("ck_workload_control_name_valid"),
        "workload_control",
        "name IN ('ai', 'agent_reach', 'external_agents', 'processing', 'commercial')",
    )
    op.execute(
        """
        INSERT INTO workload_control (name, enabled, schedule, revision) VALUES
          ('commercial', false, jsonb_build_object(
              'timezone', 'Africa/Lagos',
              'windows', jsonb_build_array(jsonb_build_object(
                  'days', jsonb_build_array(0, 1, 2, 3, 4, 5, 6),
                  'start', '01:00', 'end', '04:00'
              )),
              'max_concurrency', 1, 'max_items_per_run', 10
          ), 1)
        """
    )
    op.create_table(
        "commercial_control",
        sa.Column("singleton_id", sa.Integer(), nullable=False),
        sa.Column("global_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "explore_sponsorship_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("context_ai_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by_operator_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("singleton_id = 1", name=op.f("ck_commercial_control_singleton_only")),
        sa.CheckConstraint("revision > 0", name=op.f("ck_commercial_control_revision_positive")),
        sa.ForeignKeyConstraint(
            ["updated_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_commercial_control_updated_by_operator_id_operator"),
        ),
        sa.PrimaryKeyConstraint("singleton_id", name=op.f("pk_commercial_control")),
    )
    op.execute(
        """
        INSERT INTO commercial_control (
            singleton_id, global_enabled, explore_sponsorship_enabled, context_ai_enabled, revision
        ) VALUES (1, false, false, false, 1)
        """
    )


def downgrade() -> None:
    op.drop_table("commercial_control")
    op.execute("DELETE FROM workload_control WHERE name = 'commercial'")
    op.drop_constraint(op.f("ck_workload_control_name_valid"), "workload_control", type_="check")
    op.create_check_constraint(
        op.f("ck_workload_control_name_valid"),
        "workload_control",
        "name IN ('ai', 'agent_reach', 'external_agents', 'processing')",
    )
