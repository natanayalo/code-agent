"""Add merged_logical_activity_key to execution_plan_nodes."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260816_0049"
down_revision = "20260728_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add merged_logical_activity_key column to execution_plan_nodes (schema-only)."""
    with op.batch_alter_table("execution_plan_nodes") as batch_op:
        batch_op.add_column(
            sa.Column("merged_logical_activity_key", sa.String(length=512), nullable=True)
        )


def downgrade() -> None:
    """Drop merged_logical_activity_key column from execution_plan_nodes."""
    with op.batch_alter_table("execution_plan_nodes") as batch_op:
        batch_op.drop_column("merged_logical_activity_key")
