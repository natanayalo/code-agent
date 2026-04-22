"""Add openrouter worker type to persisted worker constraints."""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op

revision = "20260422_0018"
down_revision = "20260422_0017"
branch_labels = None
depends_on = None

WORKER_TYPE_VALUES = ("codex", "gemini", "openrouter")
PREVIOUS_WORKER_TYPE_VALUES = ("codex", "gemini")


def _check_condition(column_name: str, values: Iterable[str]) -> str:
    """Render a SQL IN check for a constrained string column."""

    rendered_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    """Allow openrouter across persisted worker fields."""

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(op.f("ck_tasks_worker_type"), type_="check")
        batch_op.drop_constraint(op.f("ck_tasks_worker_override_type"), type_="check")

    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.drop_constraint(op.f("ck_worker_runs_worker_type"), type_="check")

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_tasks_worker_type"),
            _check_condition("chosen_worker", WORKER_TYPE_VALUES),
        )
        batch_op.create_check_constraint(
            op.f("ck_tasks_worker_override_type"),
            _check_condition("worker_override", WORKER_TYPE_VALUES),
        )

    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_worker_runs_worker_type"),
            _check_condition("worker_type", WORKER_TYPE_VALUES),
        )


def downgrade() -> None:
    """Remove openrouter from persisted worker constraints."""

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(op.f("ck_tasks_worker_type"), type_="check")
        batch_op.drop_constraint(op.f("ck_tasks_worker_override_type"), type_="check")

    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.drop_constraint(op.f("ck_worker_runs_worker_type"), type_="check")

    op.execute("UPDATE tasks SET chosen_worker = 'codex' WHERE chosen_worker = 'openrouter'")
    op.execute("UPDATE tasks SET worker_override = 'codex' WHERE worker_override = 'openrouter'")
    op.execute("UPDATE worker_runs SET worker_type = 'codex' WHERE worker_type = 'openrouter'")

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_tasks_worker_type"),
            _check_condition("chosen_worker", PREVIOUS_WORKER_TYPE_VALUES),
        )
        batch_op.create_check_constraint(
            op.f("ck_tasks_worker_override_type"),
            _check_condition("worker_override", PREVIOUS_WORKER_TYPE_VALUES),
        )

    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_worker_runs_worker_type"),
            _check_condition("worker_type", PREVIOUS_WORKER_TYPE_VALUES),
        )
