"""Add durable readiness alert state and Telegram deliveries."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261027_0053"
down_revision = "20260919_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create readiness alert state and delivery tables."""
    op.create_table(
        "readiness_alert_states",
        sa.Column("reason", sa.String(length=80), primary_key=True),
        sa.Column("degraded", sa.Boolean(), nullable=False),
        sa.Column("transition_number", sa.Integer(), nullable=False),
        sa.Column("last_transition_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
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
    )
    op.create_table(
        "readiness_alert_deliveries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("reason", sa.String(length=80), nullable=False),
        sa.Column("transition_number", sa.Integer(), nullable=False),
        sa.Column("transition", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_code", sa.String(length=64), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
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
        sa.UniqueConstraint("reason", "transition_number", name="uq_readiness_delivery_transition"),
    )
    op.create_index(
        "ix_readiness_delivery_due",
        "readiness_alert_deliveries",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    """Remove readiness alert state and delivery tables."""
    op.drop_index("ix_readiness_delivery_due", table_name="readiness_alert_deliveries")
    op.drop_table("readiness_alert_deliveries")
    op.drop_table("readiness_alert_states")
