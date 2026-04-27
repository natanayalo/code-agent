"""Add persisted task specs.

Revision ID: 20260427_0020
Revises: 20260424_0019
Create Date: 2026-04-27 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op

revision = "20260427_0020"
down_revision = "20260424_0019"
branch_labels = None
depends_on = None

OLD_TIMELINE_EVENT_TYPE_VALUES = (
    "task_ingested",
    "task_classified",
    "task_planned",
    "memory_loaded",
    "worker_selected",
    "approval_requested",
    "approval_granted",
    "approval_rejected",
    "worker_dispatched",
    "worker_completed",
    "worker_failed",
    "worker_error",
    "verification_started",
    "verification_completed",
    "task_completed",
    "task_failed",
)
NEW_TIMELINE_EVENT_TYPE_VALUES = (
    "task_ingested",
    "task_classified",
    "task_planned",
    "task_spec_generated",
    "memory_loaded",
    "worker_selected",
    "approval_requested",
    "approval_granted",
    "approval_rejected",
    "worker_dispatched",
    "worker_completed",
    "worker_failed",
    "worker_error",
    "verification_started",
    "verification_completed",
    "task_completed",
    "task_failed",
)


def _check_condition(column_name: str, values: Iterable[str]) -> str:
    """Render a SQL IN check for a constrained string column."""
    rendered_values = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    """Add the task_spec payload and timeline event for TaskSpec generation."""
    op.add_column("tasks", sa.Column("task_spec", sa.JSON(), nullable=True))
    with op.batch_alter_table("task_timeline_events") as batch_op:
        batch_op.drop_constraint(op.f("ck_task_timeline_events_event_type"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_task_timeline_events_event_type"),
            _check_condition("event_type", NEW_TIMELINE_EVENT_TYPE_VALUES),
        )


def downgrade() -> None:
    """Remove persisted task specs and generated-spec timeline events."""
    op.execute("DELETE FROM task_timeline_events WHERE event_type = 'task_spec_generated'")
    with op.batch_alter_table("task_timeline_events") as batch_op:
        batch_op.drop_constraint(op.f("ck_task_timeline_events_event_type"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_task_timeline_events_event_type"),
            _check_condition("event_type", OLD_TIMELINE_EVENT_TYPE_VALUES),
        )
    op.drop_column("tasks", "task_spec")
