"""add shell worker runtime mode"""

import sqlalchemy as sa
from alembic import op

revision = "20260508_0022"
down_revision = "20260507_0021"
branch_labels = None
depends_on = None

MODES = ["native_agent", "tool_loop", "planner_only", "reviewer_only", "shell"]


def upgrade() -> None:
    # Update tasks check constraint
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_constraint("ck_tasks_worker_runtime_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_tasks_worker_runtime_mode",
            sa.column("runtime_mode").in_(MODES),
        )

    # Update worker_runs check constraint
    with op.batch_alter_table("worker_runs", schema=None) as batch_op:
        batch_op.drop_constraint("ck_worker_runs_worker_runtime_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_worker_runs_worker_runtime_mode",
            sa.column("runtime_mode").in_(MODES),
        )


def downgrade() -> None:
    OLD_MODES = ["native_agent", "tool_loop", "planner_only", "reviewer_only"]

    with op.batch_alter_table("worker_runs", schema=None) as batch_op:
        batch_op.drop_constraint("ck_worker_runs_worker_runtime_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_worker_runs_worker_runtime_mode",
            sa.column("runtime_mode").in_(OLD_MODES),
        )

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_constraint("ck_tasks_worker_runtime_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_tasks_worker_runtime_mode",
            sa.column("runtime_mode").in_(OLD_MODES),
        )
