"""Add durable Temporal activity handoff state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260713_0036"
down_revision = "20260711_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create encrypted Temporal task state storage."""
    op.create_table(
        "temporal_task_states",
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_temporal_task_states_task_id"),
    )
    op.create_index("ix_temporal_task_states_task_id", "temporal_task_states", ["task_id"])


def downgrade() -> None:
    """Remove durable Temporal activity handoff state."""
    op.drop_table("temporal_task_states")
