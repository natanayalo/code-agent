"""Add inbound delivery dedupe table."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260408_0007"
down_revision = "20260407_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create persistence for idempotent webhook delivery claims."""
    op.create_table(
        "inbound_deliveries",
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("delivery_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_inbound_deliveries_task_id_tasks"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inbound_deliveries")),
        sa.UniqueConstraint(
            "channel",
            "delivery_id",
            name="uq_inbound_deliveries_channel_delivery_id",
        ),
    )
    op.create_index(
        op.f("ix_inbound_deliveries_task_id"),
        "inbound_deliveries",
        ["task_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop inbound delivery dedupe persistence."""
    op.drop_index(op.f("ix_inbound_deliveries_task_id"), table_name="inbound_deliveries")
    op.drop_table("inbound_deliveries")
