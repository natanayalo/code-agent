"""add memory admission decisions

Revision ID: 20260704_0032
Revises: 20260704_0031
Create Date: 2026-07-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260704_0032"
down_revision = "20260704_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create inspectable memory admission decision records."""
    op.create_table(
        "memory_admission_decisions",
        sa.Column("category", sa.String(length=8), nullable=False),
        sa.Column("memory_key", sa.String(length=255), nullable=False),
        sa.Column("candidate_payload", sa.JSON(), nullable=False),
        sa.Column("decision", sa.String(length=18), nullable=False),
        sa.Column("risk_level", sa.String(length=7), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("durable_memory_id", sa.String(length=36), nullable=True),
        sa.Column("proposal_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('personal', 'project')",
            name="memory_admission_category",
        ),
        sa.CheckConstraint(
            "decision IN ('reject', 'create', 'update', 'merge', 'needs_human_review')",
            name="memory_admission_decision",
        ),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high', 'blocked')",
            name="memory_admission_risk_level",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["memory_proposals.id"],
            name="fk_memory_admission_decisions_proposal_id_memory_proposals",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_memory_admission_decisions_session_id_sessions",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_memory_admission_decisions_task_id_tasks",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_admission_decisions_decision_created_at",
        "memory_admission_decisions",
        ["decision", "created_at"],
    )
    op.create_index(
        "ix_memory_admission_decisions_proposal_id",
        "memory_admission_decisions",
        ["proposal_id"],
    )
    op.create_index(
        "ix_memory_admission_decisions_session_id",
        "memory_admission_decisions",
        ["session_id"],
    )
    op.create_index(
        "ix_memory_admission_decisions_task_id",
        "memory_admission_decisions",
        ["task_id"],
    )


def downgrade() -> None:
    """Remove memory admission decision records."""
    op.drop_index(
        "ix_memory_admission_decisions_task_id",
        table_name="memory_admission_decisions",
    )
    op.drop_index(
        "ix_memory_admission_decisions_session_id",
        table_name="memory_admission_decisions",
    )
    op.drop_index(
        "ix_memory_admission_decisions_proposal_id",
        table_name="memory_admission_decisions",
    )
    op.drop_index(
        "ix_memory_admission_decisions_decision_created_at",
        table_name="memory_admission_decisions",
    )
    op.drop_table("memory_admission_decisions")
