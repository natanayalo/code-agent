"""Constrain tasks.worker_override to known worker enum values."""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op

revision = "20260415_0010"
down_revision = "20260415_0009"
branch_labels = None
depends_on = None

WORKER_TYPE_VALUES = ("codex", "gemini")


def _check_condition(column_name: str, values: Iterable[str]) -> str:
    """Render a SQL IN check for a constrained string column."""

    rendered_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    """Add a check constraint for the optional worker override column."""

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_tasks_worker_override_type"),
            _check_condition("worker_override", WORKER_TYPE_VALUES),
        )


def downgrade() -> None:
    """Drop the worker override check constraint."""

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(op.f("ck_tasks_worker_override_type"), type_="check")
