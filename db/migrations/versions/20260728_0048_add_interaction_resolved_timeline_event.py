"""Add interaction_resolved timeline event type."""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op

revision = "20260728_0048"
down_revision = "20260728_0047"
branch_labels = None
depends_on = None

TIMELINE_EVENT_TYPE_VALUES = (
    "task_ingested",
    "task_classified",
    "task_planned",
    "task_spec_generated",
    "task_spec_and_route_generated",
    "interaction_resolved",
    "memory_loaded",
    "memory_persisted",
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
    "verification_skipped",
    "task_completed",
    "task_failed",
    "task_cancelled",
    "workspace_provisioned",
    "environment_initialized",
    "infra_failure",
    "delivery_started",
    "delivery_completed",
    "delivery_failed",
)

_PREVIOUS_TIMELINE_EVENT_TYPE_VALUES = tuple(
    value for value in TIMELINE_EVENT_TYPE_VALUES if value != "interaction_resolved"
)


def _check_condition(column_name: str, values: Iterable[str]) -> str:
    """Render a SQL IN check for a constrained string column."""
    rendered_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    """Allow interaction_resolved in persisted task timelines."""
    with op.batch_alter_table("task_timeline_events") as batch_op:
        batch_op.drop_constraint(op.f("ck_task_timeline_events_event_type"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_task_timeline_events_event_type"),
            _check_condition("event_type", TIMELINE_EVENT_TYPE_VALUES),
        )


def downgrade() -> None:
    """Remove interaction_resolved from allowed task timeline event types."""
    op.execute("DELETE FROM task_timeline_events WHERE event_type = 'interaction_resolved'")
    with op.batch_alter_table("task_timeline_events") as batch_op:
        batch_op.drop_constraint(op.f("ck_task_timeline_events_event_type"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_task_timeline_events_event_type"),
            _check_condition("event_type", _PREVIOUS_TIMELINE_EVENT_TYPE_VALUES),
        )
