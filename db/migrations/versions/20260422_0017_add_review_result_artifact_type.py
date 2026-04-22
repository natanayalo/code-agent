"""Allow persisted review artifacts in the artifact type constraint."""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op

revision = "20260422_0017"
down_revision = "20260422_0016"
branch_labels = None
depends_on = None

OLD_ARTIFACT_TYPE_VALUES = ("log", "diff", "test_report", "result_summary", "workspace")
NEW_ARTIFACT_TYPE_VALUES = (*OLD_ARTIFACT_TYPE_VALUES, "review_result")


def _check_condition(column_name: str, values: Iterable[str]) -> str:
    """Render a SQL IN check for a constrained string column."""
    rendered_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    """Expand the artifact type constraint to include structured review artifacts."""
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_constraint(op.f("ck_artifacts_artifact_type"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_artifacts_artifact_type"),
            _check_condition("artifact_type", NEW_ARTIFACT_TYPE_VALUES),
        )


def downgrade() -> None:
    """Restore the pre-review artifact type constraint."""
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_constraint(op.f("ck_artifacts_artifact_type"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_artifacts_artifact_type"),
            _check_condition("artifact_type", OLD_ARTIFACT_TYPE_VALUES),
        )
