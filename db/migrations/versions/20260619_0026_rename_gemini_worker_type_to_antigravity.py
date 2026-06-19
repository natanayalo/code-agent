"""Rename gemini worker type to antigravity in persisted constraints."""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op

revision = "20260619_0026"
down_revision = "a44580010250"
branch_labels = None
depends_on = None

WORKER_TYPE_VALUES = ("antigravity", "codex", "openrouter")
PREVIOUS_WORKER_TYPE_VALUES = ("gemini", "codex", "openrouter")


def _check_condition(column_name: str, values: Iterable[str]) -> str:
    """Render a SQL IN check for a constrained string column."""

    rendered_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    """Persist Antigravity as the canonical worker type."""

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(op.f("ck_tasks_worker_type"), type_="check")
        batch_op.drop_constraint(op.f("ck_tasks_worker_override_type"), type_="check")

    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.drop_constraint(op.f("ck_worker_runs_worker_type"), type_="check")

    op.execute("UPDATE tasks SET chosen_worker = 'antigravity' WHERE chosen_worker = 'gemini'")
    op.execute("UPDATE tasks SET worker_override = 'antigravity' WHERE worker_override = 'gemini'")
    op.execute(
        "UPDATE tasks SET chosen_profile = replace(chosen_profile, 'gemini-', 'antigravity-') "
        "WHERE chosen_profile LIKE 'gemini-%'"
    )
    op.execute("UPDATE worker_runs SET worker_type = 'antigravity' WHERE worker_type = 'gemini'")
    op.execute(
        "UPDATE worker_runs SET worker_profile = replace(worker_profile, 'gemini-', "
        "'antigravity-') WHERE worker_profile LIKE 'gemini-%'"
    )

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
    """Restore the temporary Gemini worker type vocabulary."""

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(op.f("ck_tasks_worker_type"), type_="check")
        batch_op.drop_constraint(op.f("ck_tasks_worker_override_type"), type_="check")

    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.drop_constraint(op.f("ck_worker_runs_worker_type"), type_="check")

    op.execute("UPDATE tasks SET chosen_worker = 'gemini' WHERE chosen_worker = 'antigravity'")
    op.execute("UPDATE tasks SET worker_override = 'gemini' WHERE worker_override = 'antigravity'")
    op.execute(
        "UPDATE tasks SET chosen_profile = replace(chosen_profile, 'antigravity-', 'gemini-') "
        "WHERE chosen_profile LIKE 'antigravity-%'"
    )
    op.execute("UPDATE worker_runs SET worker_type = 'gemini' WHERE worker_type = 'antigravity'")
    op.execute(
        "UPDATE worker_runs SET worker_profile = replace(worker_profile, 'antigravity-', "
        "'gemini-') WHERE worker_profile LIKE 'antigravity-%'"
    )

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
