"""Resolve Temporal commands superseded by cancellation before workflow start."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260720_0046"
down_revision = "20260719_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add an auditable non-RPC resolution outcome for outbox commands."""
    op.add_column(
        "temporal_commands",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Remove cancellation supersession evidence."""
    op.drop_column("temporal_commands", "superseded_at")
