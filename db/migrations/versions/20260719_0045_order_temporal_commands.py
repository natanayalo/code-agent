"""Preserve Temporal command delivery order within each task."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260719_0045"
down_revision = "20260719_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Backfill stable task-local ordering before enforcing it."""
    op.add_column(
        "temporal_commands",
        sa.Column("sequence_number", sa.Integer(), nullable=True),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, task_id FROM temporal_commands ORDER BY task_id, created_at, id")
    )
    sequence_by_task: dict[str, int] = {}
    for command_id, task_id in rows:
        sequence_number = sequence_by_task.get(task_id, 0) + 1
        sequence_by_task[task_id] = sequence_number
        bind.execute(
            sa.text(
                "UPDATE temporal_commands SET sequence_number = :sequence_number WHERE id = :id"
            ),
            {"id": command_id, "sequence_number": sequence_number},
        )
    with op.batch_alter_table("temporal_commands") as batch_op:
        batch_op.alter_column("sequence_number", existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint(
            "uq_temporal_commands_task_sequence", ["task_id", "sequence_number"]
        )
    op.create_index(
        "ix_temporal_commands_task_sequence",
        "temporal_commands",
        ["task_id", "sequence_number"],
    )


def downgrade() -> None:
    """Remove task-local Temporal command ordering."""
    op.drop_index("ix_temporal_commands_task_sequence", table_name="temporal_commands")
    with op.batch_alter_table("temporal_commands") as batch_op:
        batch_op.drop_constraint("uq_temporal_commands_task_sequence", type_="unique")
        batch_op.drop_column("sequence_number")
