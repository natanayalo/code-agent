"""add worker node registry"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260627_0027"
down_revision = "40fcaf6dbdad"
branch_labels = None
depends_on = None

WORKER_TYPE_VALUES = ("antigravity", "codex", "openrouter")
WORKER_NODE_STATUS_VALUES = ("active", "draining", "offline", "quarantined")


def _check_condition(column_name: str, values: tuple[str, ...]) -> str:
    rendered_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    """Create the worker-node registry table."""
    op.create_table(
        "worker_nodes",
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column("worker_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("process_identity", sa.String(length=255), nullable=True),
        sa.Column(
            "supported_profiles",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "capabilities",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("current_load", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _check_condition("worker_type", WORKER_TYPE_VALUES),
            name=op.f("ck_worker_nodes_worker_type"),
        ),
        sa.CheckConstraint(
            _check_condition("status", WORKER_NODE_STATUS_VALUES),
            name=op.f("ck_worker_nodes_worker_node_status"),
        ),
        sa.CheckConstraint("capacity > 0", name=op.f("ck_worker_nodes_worker_capacity_positive")),
        sa.CheckConstraint(
            "current_load >= 0",
            name=op.f("ck_worker_nodes_worker_load_nonnegative"),
        ),
        sa.CheckConstraint(
            "current_load <= capacity",
            name=op.f("ck_worker_nodes_worker_load_within_capacity"),
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name=op.f("ck_worker_nodes_worker_failures_nonnegative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_nodes")),
        sa.UniqueConstraint("worker_id", name=op.f("uq_worker_nodes_worker_id")),
    )

    op.create_index(
        op.f("ix_worker_nodes_worker_type"),
        "worker_nodes",
        ["worker_type"],
        unique=False,
    )
    op.create_index(op.f("ix_worker_nodes_status"), "worker_nodes", ["status"], unique=False)
    op.create_index(
        op.f("ix_worker_nodes_last_heartbeat_at"),
        "worker_nodes",
        ["last_heartbeat_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the worker-node registry table."""
    op.drop_index(op.f("ix_worker_nodes_last_heartbeat_at"), table_name="worker_nodes")
    op.drop_index(op.f("ix_worker_nodes_status"), table_name="worker_nodes")
    op.drop_index(op.f("ix_worker_nodes_worker_type"), table_name="worker_nodes")

    op.drop_table("worker_nodes")
