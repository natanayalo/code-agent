"""Add worker run retention expiry timestamp.

Revision ID: 20260422_0015
Revises: 20260421_0014
Create Date: 2026-04-22 09:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260422_0015"
down_revision = "20260421_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Record an explicit expiry for retained worker runs."""
    op.add_column(
        "worker_runs",
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_worker_runs_retention_expires_at"),
        "worker_runs",
        ["retention_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the retention expiry marker from worker runs."""
    op.drop_index(op.f("ix_worker_runs_retention_expires_at"), table_name="worker_runs")
    op.drop_column("worker_runs", "retention_expires_at")
