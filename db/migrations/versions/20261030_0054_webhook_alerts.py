"""Add destination-specific webhook alert delivery evidence."""

import sqlalchemy as sa
from alembic import op

revision = "20261030_0054"
down_revision = "20261027_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_alert_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("destination_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("transition_number", sa.Integer(), nullable=False),
        sa.Column("transition", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(36)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_code", sa.String(64)),
        sa.Column("last_http_status", sa.Integer()),
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
        sa.UniqueConstraint(
            "destination_id", "reason", "transition_number", name="uq_webhook_alert_transition"
        ),
    )
    op.create_index(
        "ix_webhook_alert_due",
        "webhook_alert_deliveries",
        ["destination_id", "status", "next_attempt_at"],
    )


def downgrade() -> None:
    # Explicitly removes webhook delivery evidence; Telegram evidence is retained.
    op.drop_index("ix_webhook_alert_due", table_name="webhook_alert_deliveries")
    op.drop_table("webhook_alert_deliveries")
