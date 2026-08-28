"""Add EphemeralSecret and Task secret_refs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260817_0050"
down_revision = "20260816_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ephemeral_secret_records table and secret_refs column to tasks."""
    op.create_table(
        "ephemeral_secret_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("handle_id", sa.String(length=255), nullable=False),
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=False),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "handle_id", name="uq_ephemeral_secrets_task_handle"),
    )
    op.create_index(
        "ix_ephemeral_secrets_task_id", "ephemeral_secret_records", ["task_id"], unique=False
    )

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column("secret_refs", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    """Drop ephemeral_secret_records table and secret_refs column from tasks."""
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("secret_refs")

    op.drop_index("ix_ephemeral_secrets_task_id", table_name="ephemeral_secret_records")
    op.drop_table("ephemeral_secret_records")
