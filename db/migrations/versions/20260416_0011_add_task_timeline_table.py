"""add task timeline table

Revision ID: 20260416_0011
Revises: 20260415_0010
Create Date: 2026-04-16 23:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260416_0011"
down_revision = "20260415_0010"
branch_labels = None
depends_on = None

TIMELINE_EVENT_TYPE_VALUES = (
    "task_ingested",
    "task_classified",
    "memory_loaded",
    "worker_selected",
    "approval_requested",
    "approval_granted",
    "approval_rejected",
    "worker_dispatched",
    "worker_completed",
    "worker_failed",
    "worker_error",
    "verification_started",
    "verification_completed",
    "task_completed",
    "task_failed",
)


def upgrade() -> None:
    op.create_table(
        "task_timeline_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sequence_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_timeline_events")),
        sa.UniqueConstraint(
            "task_id",
            "attempt_number",
            "sequence_number",
            name=op.f("uq_task_timeline_events_task_attempt_seq"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_task_timeline_events_task_id_tasks"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        op.f("ix_task_timeline_events_task_id"),
        "task_timeline_events",
        ["task_id"],
        unique=False,
    )

    with op.batch_alter_table("task_timeline_events") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_task_timeline_events_event_type"),
            sa.column("event_type").in_(TIMELINE_EVENT_TYPE_VALUES),
        )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_task_timeline_events_task_id"),
        table_name="task_timeline_events",
    )
    op.drop_table("task_timeline_events")
