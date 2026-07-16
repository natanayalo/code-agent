"""add durable node activity contract"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260716_0039"
down_revision = "20260716_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Store idempotent logical node activity state and compact result metadata."""
    with op.batch_alter_table("execution_plan_nodes") as batch_op:
        batch_op.add_column(sa.Column("latest_logical_activity_key", sa.String(512), nullable=True))
        batch_op.add_column(
            sa.Column("terminal_result_schema_version", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("terminal_result_digest", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("terminal_result_payload", sa.JSON(), nullable=True))
    with op.batch_alter_table("execution_plan_node_attempts") as batch_op:
        batch_op.add_column(sa.Column("logical_activity_key", sa.String(512), nullable=True))
        batch_op.add_column(sa.Column("claim_token", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("claim_generation", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("result_schema_version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("result_digest", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("result_payload", sa.JSON(), nullable=True))
    op.execute(
        "UPDATE execution_plan_node_attempts SET logical_activity_key = "
        "'legacy:' || plan_node_id || ':' || attempt_number"
    )
    with op.batch_alter_table("execution_plan_node_attempts") as batch_op:
        batch_op.create_unique_constraint(
            "uq_plan_node_attempt_activity_key", ["plan_node_id", "logical_activity_key"]
        )


def downgrade() -> None:
    """Remove node activity persistence fields."""
    with op.batch_alter_table("execution_plan_node_attempts") as batch_op:
        batch_op.drop_constraint("uq_plan_node_attempt_activity_key", type_="unique")
        batch_op.drop_column("result_payload")
        batch_op.drop_column("result_digest")
        batch_op.drop_column("result_schema_version")
        batch_op.drop_column("claim_expires_at")
        batch_op.drop_column("heartbeat_at")
        batch_op.drop_column("claim_generation")
        batch_op.drop_column("claim_token")
        batch_op.drop_column("logical_activity_key")
    with op.batch_alter_table("execution_plan_nodes") as batch_op:
        batch_op.drop_column("terminal_result_payload")
        batch_op.drop_column("terminal_result_digest")
        batch_op.drop_column("terminal_result_schema_version")
        batch_op.drop_column("latest_logical_activity_key")
