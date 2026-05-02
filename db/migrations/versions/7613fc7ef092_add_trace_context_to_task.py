"""add trace_context to task"""

revision = "7613fc7ef092"
down_revision = "d5d89f9b407a"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # noqa: E402
from alembic import op  # noqa: E402


def upgrade() -> None:
    op.add_column(  # noqa: E501
        "tasks", sa.Column("trace_context", sa.JSON(), nullable=False, server_default="{}")
    )


def downgrade() -> None:
    op.drop_column("tasks", "trace_context")
