"""M20.6 Draft PR and CI Repair Loop"""

import sqlalchemy as sa
from alembic import op

revision = "a03f79ba2763"
down_revision = "20260627_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("repair_for_task_id", sa.String(length=36), nullable=True))
        batch_op.create_index(op.f("ix_tasks_repair_for_task_id"), ["repair_for_task_id"])
        batch_op.create_foreign_key(
            op.f("fk_tasks_repair_for_task_id_tasks"),
            "tasks",
            ["repair_for_task_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.add_column(sa.Column("delivery_metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.drop_column("delivery_metadata")

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_tasks_repair_for_task_id_tasks"),
            type_="foreignkey",
        )
        batch_op.drop_index(op.f("ix_tasks_repair_for_task_id"))
        batch_op.drop_column("repair_for_task_id")
