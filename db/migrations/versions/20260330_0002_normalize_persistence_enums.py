"""Add constrained enums for persisted statuses, worker types, and artifact types."""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op

revision = "20260330_0002"
down_revision = "20260329_0001"
branch_labels = None
depends_on = None

SESSION_STATUS_VALUES = ("active", "closed")
TASK_STATUS_VALUES = ("pending", "in_progress", "completed", "failed", "cancelled")
WORKER_TYPE_VALUES = ("claude", "codex")
WORKER_RUN_STATUS_VALUES = ("queued", "running", "success", "failure", "error", "cancelled")
ARTIFACT_TYPE_VALUES = ("log", "diff", "test_report", "result_summary")


def _check_condition(column_name: str, values: Iterable[str]) -> str:
    """Render a SQL IN check for a constrained string column."""

    rendered_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    """Constrain persisted enum-like string columns with check constraints."""

    with op.batch_alter_table("sessions") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_sessions_session_status"),
            _check_condition("status", SESSION_STATUS_VALUES),
        )

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_tasks_task_status"),
            _check_condition("status", TASK_STATUS_VALUES),
        )
        batch_op.create_check_constraint(
            op.f("ck_tasks_worker_type"),
            _check_condition("chosen_worker", WORKER_TYPE_VALUES),
        )

    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_worker_runs_worker_type"),
            _check_condition("worker_type", WORKER_TYPE_VALUES),
        )
        batch_op.create_check_constraint(
            op.f("ck_worker_runs_worker_run_status"),
            _check_condition("status", WORKER_RUN_STATUS_VALUES),
        )

    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_artifacts_artifact_type"),
            _check_condition("artifact_type", ARTIFACT_TYPE_VALUES),
        )


def downgrade() -> None:
    """Remove constrained enum-like checks from persisted string columns."""

    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_constraint(op.f("ck_artifacts_artifact_type"), type_="check")

    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.drop_constraint(op.f("ck_worker_runs_worker_run_status"), type_="check")
        batch_op.drop_constraint(op.f("ck_worker_runs_worker_type"), type_="check")

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(op.f("ck_tasks_worker_type"), type_="check")
        batch_op.drop_constraint(op.f("ck_tasks_task_status"), type_="check")

    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_constraint(op.f("ck_sessions_session_status"), type_="check")
