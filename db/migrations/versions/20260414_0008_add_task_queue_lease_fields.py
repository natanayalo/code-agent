"""Add task queue lease/retry fields for split api/worker runtime."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260414_0008"
down_revision = "20260408_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add queue and lease state columns to tasks."""
    op.add_column("tasks", sa.Column("callback_url", sa.String(length=2048), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "tasks",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column("tasks", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("lease_owner", sa.String(length=255), nullable=True))
    op.add_column("tasks", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("last_error", sa.Text(), nullable=True))

    op.create_index(op.f("ix_tasks_next_attempt_at"), "tasks", ["next_attempt_at"], unique=False)
    op.create_index(op.f("ix_tasks_lease_expires_at"), "tasks", ["lease_expires_at"], unique=False)


def downgrade() -> None:
    """Drop queue and lease state columns from tasks."""
    op.drop_index(op.f("ix_tasks_lease_expires_at"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_next_attempt_at"), table_name="tasks")
    op.drop_column("tasks", "last_error")
    op.drop_column("tasks", "lease_expires_at")
    op.drop_column("tasks", "lease_owner")
    op.drop_column("tasks", "next_attempt_at")
    op.drop_column("tasks", "max_attempts")
    op.drop_column("tasks", "attempt_count")
    op.drop_column("tasks", "callback_url")
