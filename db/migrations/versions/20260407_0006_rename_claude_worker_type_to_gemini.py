"""Rename claude worker type to gemini in check constraints."""

from __future__ import annotations

from alembic import op

revision = "20260407_0006"
down_revision = "20260406_0005"
branch_labels = None
depends_on = None

OLD_WORKER_TYPE_VALUES = ("claude", "codex")
NEW_WORKER_TYPE_VALUES = ("gemini", "codex")


def _check_condition(column_name: str, values: tuple[str, ...]) -> str:
    rendered_values = ", ".join(f"'{v}'" for v in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    # Update any existing rows that reference the old value.
    op.execute("UPDATE tasks SET chosen_worker = 'gemini' WHERE chosen_worker = 'claude'")
    op.execute("UPDATE worker_runs SET worker_type = 'gemini' WHERE worker_type = 'claude'")

    # Re-create check constraints with the new enum vocabulary.
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("ck_tasks_worker_type")
        batch_op.create_check_constraint(
            op.f("ck_tasks_worker_type"),
            _check_condition("chosen_worker", NEW_WORKER_TYPE_VALUES),
        )

    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.drop_constraint("ck_worker_runs_worker_type")
        batch_op.create_check_constraint(
            op.f("ck_worker_runs_worker_type"),
            _check_condition("worker_type", NEW_WORKER_TYPE_VALUES),
        )


def downgrade() -> None:
    op.execute("UPDATE tasks SET chosen_worker = 'claude' WHERE chosen_worker = 'gemini'")
    op.execute("UPDATE worker_runs SET worker_type = 'claude' WHERE worker_type = 'gemini'")

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("ck_tasks_worker_type")
        batch_op.create_check_constraint(
            op.f("ck_tasks_worker_type"),
            _check_condition("chosen_worker", OLD_WORKER_TYPE_VALUES),
        )

    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.drop_constraint("ck_worker_runs_worker_type")
        batch_op.create_check_constraint(
            op.f("ck_worker_runs_worker_type"),
            _check_condition("worker_type", OLD_WORKER_TYPE_VALUES),
        )
