"""Add milestone readiness and autonomy policy persistence."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260721_0047"
down_revision = "20260720_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create milestone and advisory readiness-assessment tables."""
    milestone_status = sa.Enum(
        "planned",
        "active",
        "completed",
        name="milestone_status",
        native_enum=False,
        create_constraint=True,
    )
    autonomy_mode = sa.Enum(
        "human_led",
        "agent_led_approval_gated",
        "autonomous_delivery",
        name="milestone_autonomy_mode",
        native_enum=False,
        create_constraint=True,
    )
    readiness_status = sa.Enum(
        "queued",
        "reviewing",
        "pending_approval",
        "approved",
        "rejected",
        "failed",
        "superseded",
        name="milestone_readiness_status",
        native_enum=False,
        create_constraint=True,
    )
    op.create_table(
        "milestones",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", milestone_status, nullable=False),
        sa.Column("successor_id", sa.String(length=36), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "active_autonomy_mode", autonomy_mode, nullable=False, server_default="human_led"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_milestones_key"),
        sa.UniqueConstraint("sequence"),
        sa.ForeignKeyConstraint(["successor_id"], ["milestones.id"]),
    )
    op.create_table(
        "milestone_readiness_assessments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("completed_milestone_id", sa.String(length=36), nullable=False),
        sa.Column("next_milestone_id", sa.String(length=36), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", readiness_status, nullable=False),
        sa.Column("evidence_snapshot", sa.JSON(), nullable=False),
        sa.Column("rubric", sa.JSON(), nullable=False),
        sa.Column("reviewer_narrative", sa.Text(), nullable=True),
        sa.Column("recommended_mode", autonomy_mode, nullable=True),
        sa.Column("approved_mode", autonomy_mode, nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "completed_milestone_id", "generation", name="uq_readiness_completed_generation"
        ),
        sa.ForeignKeyConstraint(["completed_milestone_id"], ["milestones.id"]),
        sa.ForeignKeyConstraint(["next_milestone_id"], ["milestones.id"]),
    )
    op.create_index(
        "ix_readiness_next_milestone_status",
        "milestone_readiness_assessments",
        ["next_milestone_id", "status"],
    )
    op.add_column("tasks", sa.Column("milestone_id", sa.String(length=36), nullable=True))
    op.create_index("ix_tasks_milestone_id", "tasks", ["milestone_id"])


def downgrade() -> None:
    """Remove milestone readiness persistence."""
    op.drop_index("ix_tasks_milestone_id", table_name="tasks")
    op.drop_column("tasks", "milestone_id")
    op.drop_index(
        "ix_readiness_next_milestone_status", table_name="milestone_readiness_assessments"
    )
    op.drop_table("milestone_readiness_assessments")
    op.drop_table("milestones")
