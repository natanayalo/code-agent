"""add task_cancelled timeline event type

Revision ID: 20260507_0021
Revises: fe60a3e57592
Create Date: 2026-05-07 12:30:00.000000

"""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op

revision = "20260507_0021"
down_revision = "fe60a3e57592"
branch_labels = None
depends_on = None

TIMELINE_EVENT_TYPE_VALUES = (
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
    "task_cancelled",
)

_PREVIOUS_TIMELINE_EVENT_TYPE_VALUES = tuple(
    value for value in TIMELINE_EVENT_TYPE_VALUES if value != "task_cancelled"
)


def _check_condition(column_name: str, values: Iterable[str]) -> str:
    """Render a SQL IN check for a constrained string column."""
    rendered_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    """Allow the task_cancelled timeline event in persisted task timelines."""
    with op.batch_alter_table("task_timeline_events") as batch_op:
        batch_op.drop_constraint("ck_task_timeline_events_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_task_timeline_events_event_type",
            _check_condition("event_type", TIMELINE_EVENT_TYPE_VALUES),
        )


def downgrade() -> None:
    """Remove task_cancelled from allowed persisted task timeline event types."""
    op.execute("DELETE FROM task_timeline_events WHERE event_type = 'task_cancelled'")
    with op.batch_alter_table("task_timeline_events") as batch_op:
        batch_op.drop_constraint("ck_task_timeline_events_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_task_timeline_events_event_type",
            _check_condition("event_type", _PREVIOUS_TIMELINE_EVENT_TYPE_VALUES),
        )
