"""Add idempotency identity for product timeline events."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260714_0037"
down_revision = "20260713_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add an optional per-task event key with a uniqueness guarantee."""
    with op.batch_alter_table("task_timeline_events") as batch_op:
        batch_op.add_column(sa.Column("event_key", sa.String(length=255)))
        batch_op.create_unique_constraint(
            "uq_task_timeline_events_task_event_key", ["task_id", "event_key"]
        )


def downgrade() -> None:
    """Remove timeline event idempotency identity."""
    with op.batch_alter_table("task_timeline_events") as batch_op:
        batch_op.drop_constraint("uq_task_timeline_events_task_event_key", type_="unique")
        batch_op.drop_column("event_key")
