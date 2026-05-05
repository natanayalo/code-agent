"""add profile and runtime metadata"""

import sqlalchemy as sa
from alembic import op

revision = "fe60a3e57592"
down_revision = "7613fc7ef092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("chosen_profile", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "runtime_mode",
                sa.Enum(
                    "native_agent",
                    "tool_loop",
                    "planner_only",
                    "reviewer_only",
                    name="worker_runtime_mode",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=True,
            )
        )

    with op.batch_alter_table("worker_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("worker_profile", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "runtime_mode",
                sa.Enum(
                    "native_agent",
                    "tool_loop",
                    "planner_only",
                    "reviewer_only",
                    name="worker_runtime_mode",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("worker_runs", schema=None) as batch_op:
        batch_op.drop_column("runtime_mode")
        batch_op.drop_column("worker_profile")

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("runtime_mode")
        batch_op.drop_column("chosen_profile")
