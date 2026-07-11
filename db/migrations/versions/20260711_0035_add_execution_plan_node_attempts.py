"""add durable execution plan node attempts"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260711_0035"
down_revision = "20260710_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create append-only per-node attempt evidence."""
    op.create_table(
        "execution_plan_node_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plan_node_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("worker_run_id", sa.String(length=36), nullable=True),
        sa.Column("task_trace_id", sa.String(length=64), nullable=True),
        sa.Column("worker_type", sa.String(length=50), nullable=True),
        sa.Column("worker_profile", sa.String(length=255), nullable=True),
        sa.Column("runtime_mode", sa.String(length=50), nullable=True),
        sa.Column("workspace_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="started"),
        sa.Column("failure_kind", sa.String(length=50), nullable=True),
        sa.Column("effective_input_summary", sa.JSON(), nullable=False),
        sa.Column("effective_input_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plan_node_id"], ["execution_plan_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_node_id", "attempt_number", name="uq_plan_node_attempt_number"),
    )
    op.create_index(
        "ix_execution_plan_node_attempts_plan_node_id",
        "execution_plan_node_attempts",
        ["plan_node_id"],
    )
    op.create_index(
        "ix_execution_plan_node_attempts_worker_run_id",
        "execution_plan_node_attempts",
        ["worker_run_id"],
    )
    op.create_index(
        "ix_execution_plan_node_attempts_task_trace_id",
        "execution_plan_node_attempts",
        ["task_trace_id"],
    )


def downgrade() -> None:
    """Remove durable per-node attempt evidence."""
    op.drop_table("execution_plan_node_attempts")
