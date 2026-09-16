"""Add context_envelope to artifact_type constraint.

WARNING: downgrade is intentionally destructive and deletes all persisted
context_envelope artifact rows before restoring the previous constraint.

Revision ID: 20260913_0051
Revises: 20260817_0050
Create Date: 2026-09-13 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op

revision = "20260913_0051"
down_revision = "20260817_0050"
branch_labels = None
depends_on = None

OLD_ARTIFACT_TYPE_VALUES = (
    "log",
    "diff",
    "test_report",
    "result_summary",
    "workspace",
    "review_result",
    "independent_review_result",
)
NEW_ARTIFACT_TYPE_VALUES = (*OLD_ARTIFACT_TYPE_VALUES, "context_envelope")


def _check_condition(column_name: str, values: Iterable[str]) -> str:
    """Render a SQL IN check for a constrained string column."""
    rendered_values = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    """Expand the artifact type constraint to include context envelope artifacts."""
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_constraint(op.f("ck_artifacts_artifact_type"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_artifacts_artifact_type"),
            _check_condition("artifact_type", NEW_ARTIFACT_TYPE_VALUES),
        )


def downgrade() -> None:
    """Restore the pre-context-envelope artifact type constraint."""
    op.execute("DELETE FROM artifacts WHERE artifact_type = 'context_envelope'")
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_constraint(op.f("ck_artifacts_artifact_type"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_artifacts_artifact_type"),
            _check_condition("artifact_type", OLD_ARTIFACT_TYPE_VALUES),
        )
