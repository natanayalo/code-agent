"""Add Proposal model

Revision ID: c0ffee123456
Revises: 7d9ec93c991e
Create Date: 2026-06-14 09:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c0ffee123456"
down_revision = "7d9ec93c991e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposals",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending_review",
                "accepted",
                "rejected",
                "implemented",
                name="proposal_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_proposals_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_proposals_task_id_tasks"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposals")),
    )
    op.create_index(
        "ix_proposals_session_id",
        "proposals",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_proposals_task_id",
        "proposals",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "ix_proposals_status",
        "proposals",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_proposals_status", table_name="proposals")
    op.drop_index("ix_proposals_task_id", table_name="proposals")
    op.drop_index("ix_proposals_session_id", table_name="proposals")
    op.drop_table("proposals")
