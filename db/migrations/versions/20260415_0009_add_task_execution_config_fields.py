"""Persist task execution configuration for queued worker runs.

Revision ID: 20260415_0009
Revises: 20260414_0008
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260415_0009"
down_revision = "20260414_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add worker override, constraints, and budget columns to persisted tasks."""
    op.add_column("tasks", sa.Column("worker_override", sa.String(length=50), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("constraints", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "tasks",
        sa.Column("budget", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    """Drop queued execution-configuration columns from persisted tasks."""
    op.drop_column("tasks", "budget")
    op.drop_column("tasks", "constraints")
    op.drop_column("tasks", "worker_override")
