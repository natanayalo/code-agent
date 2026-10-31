"""Add auditable readiness incident acknowledgments."""

import sqlalchemy as sa
from alembic import op

revision = "20261031_0055"
down_revision = "20261030_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "readiness_alert_acknowledgments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("transition_number", sa.Integer(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_by", sa.String(80), nullable=False),
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
        sa.UniqueConstraint("reason", "transition_number", name="uq_readiness_ack_incident"),
    )


def downgrade() -> None:
    op.drop_table("readiness_alert_acknowledgments")
