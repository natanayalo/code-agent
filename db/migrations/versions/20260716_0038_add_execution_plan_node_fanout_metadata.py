"""add execution-plan fan-out metadata"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260716_0038"
down_revision = "20260714_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist explicit node scheduling and aggregation semantics."""
    with op.batch_alter_table("execution_plan_nodes") as batch_op:
        batch_op.add_column(
            sa.Column(
                "aggregation_role",
                sa.String(length=50),
                nullable=False,
                server_default="mutation",
            )
        )
        batch_op.add_column(
            sa.Column(
                "execution_mode",
                sa.String(length=50),
                nullable=False,
                server_default="mutable",
            )
        )
        batch_op.add_column(
            sa.Column("parallel_safe", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.execute(
        """
        UPDATE execution_plan_nodes
        SET aggregation_role = CASE
            WHEN node_kind = 'inspect' THEN 'context'
            WHEN node_kind IN ('verify', 'review') THEN 'validation'
            WHEN node_kind = 'aggregate' THEN 'final'
            ELSE 'mutation'
        END
        """
    )


def downgrade() -> None:
    """Remove explicit node scheduling and aggregation semantics."""
    with op.batch_alter_table("execution_plan_nodes") as batch_op:
        batch_op.drop_column("parallel_safe")
        batch_op.drop_column("execution_mode")
        batch_op.drop_column("aggregation_role")
