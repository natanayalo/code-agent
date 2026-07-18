"""add durable execution capacity permits"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260718_0040"
down_revision = "20260716_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_capacity_permits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("queue_name", sa.String(length=255), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=512), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("queue_name", "slot_index", name="uq_execution_capacity_slot"),
    )
    op.create_index(
        "ix_execution_capacity_permits_queue_name",
        "execution_capacity_permits",
        ["queue_name"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_capacity_permits_queue_name",
        table_name="execution_capacity_permits",
    )
    op.drop_table("execution_capacity_permits")
