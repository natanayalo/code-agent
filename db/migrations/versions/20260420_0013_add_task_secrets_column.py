"""Add task secrets column"""

import sqlalchemy as sa
from alembic import op

revision = "20260420_0013"
down_revision = "20260418_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add secrets JSON column to tasks table
    op.add_column(
        "tasks",
        sa.Column(
            "secrets",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    # Remove secrets JSON column from tasks table
    op.drop_column("tasks", "secrets")
