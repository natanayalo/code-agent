"""Add immutable orchestration-runtime evidence to tasks and worker runs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260718_0042"
down_revision = "20260718_0041"
branch_labels = None
depends_on = None

_RUNTIME_COLUMN = sa.String(length=8)
_RUNTIME_CHECK = "orchestration_runtime IN ('temporal', 'legacy')"


def upgrade() -> None:
    """Add nullable markers and backfill only positively identified Temporal rows."""

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("orchestration_runtime", _RUNTIME_COLUMN, nullable=True))
        batch_op.create_check_constraint("orchestration_runtime", _RUNTIME_CHECK)
        batch_op.create_index("ix_tasks_orchestration_runtime", ["orchestration_runtime"])
    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.add_column(sa.Column("orchestration_runtime", _RUNTIME_COLUMN, nullable=True))
        batch_op.create_check_constraint("orchestration_runtime", _RUNTIME_CHECK)
        batch_op.create_index("ix_worker_runs_orchestration_runtime", ["orchestration_runtime"])

    op.execute(
        """
        UPDATE tasks
        SET orchestration_runtime = 'temporal'
        WHERE id IN (SELECT task_id FROM temporal_task_states)
        """
    )
    op.execute(
        """
        UPDATE worker_runs
        SET orchestration_runtime = 'temporal'
        WHERE task_id IN (
            SELECT id FROM tasks WHERE orchestration_runtime = 'temporal'
        )
        """
    )


def downgrade() -> None:
    """Remove runtime observability columns after rolling back this slice."""

    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.drop_index("ix_worker_runs_orchestration_runtime")
        batch_op.drop_constraint("orchestration_runtime", type_="check")
        batch_op.drop_column("orchestration_runtime")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_index("ix_tasks_orchestration_runtime")
        batch_op.drop_constraint("orchestration_runtime", type_="check")
        batch_op.drop_column("orchestration_runtime")
