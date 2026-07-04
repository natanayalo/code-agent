"""add reviewable memory proposals

Revision ID: 20260704_0031
Revises: 20260703_0030
Create Date: 2026-07-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260704_0031"
down_revision = "20260703_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create reviewable memory proposals separate from task-producing proposals."""
    _create_memory_proposals_table()
    _create_memory_proposals_indexes()


def _create_memory_proposals_table() -> None:
    """Create the memory proposal review table."""
    op.create_table(
        "memory_proposals",
        sa.Column("category", sa.String(length=8), nullable=False),
        sa.Column("repo_url", sa.String(length=512), nullable=True),
        sa.Column("memory_key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=True),
        sa.Column("requires_verification", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=14), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("accepted_memory_id", sa.String(length=36), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('personal', 'project')",
            name="memory_proposal_category",
        ),
        sa.CheckConstraint(
            "status IN ('pending_review', 'accepted', 'rejected')",
            name="memory_proposal_status",
        ),
        sa.CheckConstraint(
            "((category = 'project' AND repo_url IS NOT NULL) OR "
            "(category = 'personal' AND repo_url IS NULL))",
            name="category_repo_url",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="confidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_memory_proposals_session_id_sessions",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_memory_proposals_task_id_tasks",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_memory_proposals_indexes() -> None:
    """Create memory proposal filter indexes."""
    op.create_index(
        "ix_memory_proposals_category",
        "memory_proposals",
        ["category"],
    )
    op.create_index(
        "ix_memory_proposals_category_status",
        "memory_proposals",
        ["category", "status"],
    )
    op.create_index(
        "ix_memory_proposals_repo_url",
        "memory_proposals",
        ["repo_url"],
    )
    op.create_index(
        "ix_memory_proposals_session_id",
        "memory_proposals",
        ["session_id"],
    )
    op.create_index(
        "ix_memory_proposals_status",
        "memory_proposals",
        ["status"],
    )
    op.create_index(
        "ix_memory_proposals_status_created_at",
        "memory_proposals",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_memory_proposals_task_id",
        "memory_proposals",
        ["task_id"],
    )


def downgrade() -> None:
    """Remove reviewable memory proposals."""
    op.drop_index("ix_memory_proposals_task_id", table_name="memory_proposals")
    op.drop_index("ix_memory_proposals_status_created_at", table_name="memory_proposals")
    op.drop_index("ix_memory_proposals_status", table_name="memory_proposals")
    op.drop_index("ix_memory_proposals_session_id", table_name="memory_proposals")
    op.drop_index("ix_memory_proposals_repo_url", table_name="memory_proposals")
    op.drop_index("ix_memory_proposals_category_status", table_name="memory_proposals")
    op.drop_index("ix_memory_proposals_category", table_name="memory_proposals")
    op.drop_table("memory_proposals")
