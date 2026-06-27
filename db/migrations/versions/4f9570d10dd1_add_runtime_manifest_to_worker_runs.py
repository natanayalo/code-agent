"""add runtime_manifest to worker_runs"""

import sqlalchemy as sa
from alembic import op

revision = "4f9570d10dd1"
down_revision = "20260619_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worker_runs", sa.Column("runtime_manifest", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("worker_runs", "runtime_manifest")
