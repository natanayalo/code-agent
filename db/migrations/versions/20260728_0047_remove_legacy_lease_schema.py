"""Remove legacy task leases and worker load accounting."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_0047"
down_revision = "20260720_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Remove schema state owned only by the retired Postgres scheduler."""
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_index(op.f("ix_tasks_lease_expires_at"))
        batch_op.drop_index(op.f("ix_tasks_next_attempt_at"))
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_owner")
        batch_op.drop_column("next_attempt_at")

    with op.batch_alter_table("worker_nodes") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_worker_nodes_worker_load_within_capacity"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_worker_nodes_worker_load_nonnegative"),
            type_="check",
        )
        batch_op.drop_column("current_load")


def downgrade() -> None:
    """Restore legacy schema shape without attempting to reconstruct dropped values."""
    with op.batch_alter_table("worker_nodes") as batch_op:
        batch_op.add_column(
            sa.Column(
                "current_load",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_worker_nodes_worker_load_nonnegative"),
            "current_load >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_worker_nodes_worker_load_within_capacity"),
            "current_load <= capacity",
        )

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("lease_owner", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            op.f("ix_tasks_next_attempt_at"),
            ["next_attempt_at"],
            unique=False,
        )
        batch_op.create_index(
            op.f("ix_tasks_lease_expires_at"),
            ["lease_expires_at"],
            unique=False,
        )
