"""Add worker run observability fields"""

import sqlalchemy as sa
from alembic import op

revision = "20260406_0005"
down_revision = "15bca5e3069c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.add_column(sa.Column("session_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("requested_permission", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("budget_usage", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("verifier_outcome", sa.JSON(), nullable=True))
        batch_op.create_index(op.f("ix_worker_runs_session_id"), ["session_id"], unique=False)
        batch_op.create_foreign_key(
            op.f("fk_worker_runs_session_id_sessions"),
            "sessions",
            ["session_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("worker_runs") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_worker_runs_session_id_sessions"),
            type_="foreignkey",
        )
        batch_op.drop_index(op.f("ix_worker_runs_session_id"))
        batch_op.drop_column("verifier_outcome")
        batch_op.drop_column("budget_usage")
        batch_op.drop_column("requested_permission")
        batch_op.drop_column("session_id")
