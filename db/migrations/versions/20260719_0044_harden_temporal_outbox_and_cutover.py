"""Harden Temporal command delivery and persist runtime cutovers."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260719_0044"
down_revision = "20260718_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add fenced outbox delivery fields and immutable cutover evidence."""
    op.add_column(
        "temporal_commands", sa.Column("claim_token", sa.String(length=36), nullable=True)
    )
    op.add_column(
        "temporal_commands",
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "temporal_commands",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "temporal_commands",
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_temporal_commands_claim_token", "temporal_commands", ["claim_token"])
    op.create_index(
        "ix_temporal_commands_next_attempt_at", "temporal_commands", ["next_attempt_at"]
    )
    op.create_table(
        "runtime_cutovers",
        sa.Column("cutover_name", sa.String(length=64), nullable=False),
        sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("release_identifier", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("cutover_name"),
    )


def downgrade() -> None:
    """Remove cutover evidence and hardened command delivery fields."""
    op.drop_table("runtime_cutovers")
    op.drop_index("ix_temporal_commands_next_attempt_at", table_name="temporal_commands")
    op.drop_index("ix_temporal_commands_claim_token", table_name="temporal_commands")
    op.drop_column("temporal_commands", "dead_lettered_at")
    op.drop_column("temporal_commands", "next_attempt_at")
    op.drop_column("temporal_commands", "claim_expires_at")
    op.drop_column("temporal_commands", "claim_token")
