"""Add task secrets_encrypted column

Revision ID: 20260421_0014
Revises: 20260420_0013
Create Date: 2026-04-21 10:45:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260421_0014"
down_revision = "20260420_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add secrets_encrypted boolean column to tasks table
    op.add_column(
        "tasks",
        sa.Column(
            "secrets_encrypted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    # Remove secrets_encrypted boolean column from tasks table
    op.drop_column("tasks", "secrets_encrypted")
