"""add node contracts and outcome evidence to execution plans"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260710_0034"
down_revision = "20260704_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add persisted M24 node contracts and outcome evidence."""
    with op.batch_alter_table("execution_plan_nodes") as batch_op:
        batch_op.add_column(sa.Column("task_spec", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("node_kind", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("worker_run_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("result_summary", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("failure_kind", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("verification_outcome", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("changed_files", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("output_artifacts", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            "fk_execution_plan_nodes_worker_run_id_worker_runs",
            "worker_runs",
            ["worker_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_execution_plan_nodes_worker_run_id", ["worker_run_id"], unique=False
        )


def downgrade() -> None:
    """Remove persisted M24 node contracts and outcome evidence."""
    with op.batch_alter_table("execution_plan_nodes") as batch_op:
        batch_op.drop_index("ix_execution_plan_nodes_worker_run_id")
        batch_op.drop_constraint(
            "fk_execution_plan_nodes_worker_run_id_worker_runs", type_="foreignkey"
        )
        for column in (
            "last_attempt_at",
            "output_artifacts",
            "changed_files",
            "verification_outcome",
            "failure_kind",
            "result_summary",
            "worker_run_id",
            "node_kind",
            "task_spec",
        ):
            batch_op.drop_column(column)
