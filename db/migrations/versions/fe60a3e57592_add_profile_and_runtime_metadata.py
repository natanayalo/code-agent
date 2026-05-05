"""add profile and runtime metadata"""

import sqlalchemy as sa
from alembic import op

revision = "fe60a3e57592"
down_revision = "7613fc7ef092"
branch_labels = None
depends_on = None


# NOTE: The following indices and constraints already exist in the migration history
# and should not be re-added here:
# - ix_tasks_next_attempt_at (added in 20260414_0008)
# - ix_tasks_lease_expires_at (added in 20260414_0008)
# - uq_task_timeline_events_task_attempt_seq (added in 20260416_0011)


def upgrade() -> None:
    runtime_mode_enum = sa.Enum(
        "native_agent",
        "tool_loop",
        "planner_only",
        "reviewer_only",
        name="worker_runtime_mode",
        native_enum=False,
        create_constraint=False,  # We will create it manually to control the name
    )

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("chosen_profile", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("runtime_mode", runtime_mode_enum, nullable=True))
        batch_op.create_check_constraint(
            "ck_tasks_worker_runtime_mode",
            sa.column("runtime_mode").in_(
                ["native_agent", "tool_loop", "planner_only", "reviewer_only"]
            ),
        )

    with op.batch_alter_table("worker_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("worker_profile", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("runtime_mode", runtime_mode_enum, nullable=True))
        batch_op.create_check_constraint(
            "ck_worker_runs_worker_runtime_mode",
            sa.column("runtime_mode").in_(
                ["native_agent", "tool_loop", "planner_only", "reviewer_only"]
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("worker_runs", schema=None) as batch_op:
        batch_op.drop_constraint("ck_worker_runs_worker_runtime_mode", type_="check")
        batch_op.drop_column("runtime_mode")
        batch_op.drop_column("worker_profile")

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_constraint("ck_tasks_worker_runtime_mode", type_="check")
        batch_op.drop_column("runtime_mode")
        batch_op.drop_column("chosen_profile")
