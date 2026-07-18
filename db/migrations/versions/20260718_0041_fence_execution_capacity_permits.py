"""fence execution capacity permits with acquisition tokens"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260718_0041"
down_revision = "20260718_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the token used to fence permit heartbeat and release operations."""
    op.add_column(
        "execution_capacity_permits",
        sa.Column("lease_token", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Remove the permit acquisition token."""
    op.drop_column("execution_capacity_permits", "lease_token")
