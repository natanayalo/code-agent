"""Add transactional Temporal command outbox."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260718_0043"
down_revision = "20260718_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create durable, idempotent Temporal command records."""
    op.create_table(
        "temporal_commands",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("command_type", sa.String(length=32), nullable=False),
        sa.Column("command_key", sa.String(length=512), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_key", name="uq_temporal_commands_command_key"),
    )
    op.create_index("ix_temporal_commands_task_id", "temporal_commands", ["task_id"])
    op.create_index("ix_temporal_commands_command_type", "temporal_commands", ["command_type"])


def downgrade() -> None:
    """Remove the outbox when rolling back this compatibility-safe slice."""
    op.drop_index("ix_temporal_commands_command_type", table_name="temporal_commands")
    op.drop_index("ix_temporal_commands_task_id", table_name="temporal_commands")
    op.drop_table("temporal_commands")
