"""add task_planned timeline event type

Revision ID: 20260422_0016
Revises: 20260422_0015
Create Date: 2026-04-22 00:30:00.000000

"""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op

revision = "20260422_0016"
down_revision = "20260422_0015"
branch_labels = None
depends_on = None

TIMELINE_EVENT_TYPE_VALUES = (
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

_PREVIOUS_TIMELINE_EVENT_TYPE_VALUES = tuple(
    value for value in TIMELINE_EVENT_TYPE_VALUES if value != "task_planned"
)


def _check_condition(column_name: str, values: Iterable[str]) -> str:
    """Render a SQL IN check for a constrained string column."""
    rendered_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    """Allow the task_planned timeline event in persisted task timelines."""
    with op.batch_alter_table("task_timeline_events") as batch_op:
        batch_op.drop_constraint(op.f("ck_task_timeline_events_event_type"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_task_timeline_events_event_type"),
            _check_condition("event_type", TIMELINE_EVENT_TYPE_VALUES),
        )


def downgrade() -> None:
    """Remove task_planned from allowed persisted task timeline event types."""
    with op.batch_alter_table("task_timeline_events") as batch_op:
        batch_op.drop_constraint(op.f("ck_task_timeline_events_event_type"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_task_timeline_events_event_type"),
            _check_condition("event_type", _PREVIOUS_TIMELINE_EVENT_TYPE_VALUES),
        )
