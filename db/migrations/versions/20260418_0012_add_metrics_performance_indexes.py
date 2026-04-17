"""add metrics performance indexes

Revision ID: 20260418_0012
Revises: 20260416_0011
Create Date: 2026-04-18 00:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260418_0012"
down_revision = "20260416_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tasks
    op.create_index(op.f("ix_tasks_status"), "tasks", ["status"], unique=False)
    op.create_index(op.f("ix_tasks_attempt_count"), "tasks", ["attempt_count"], unique=False)
    op.create_index(op.f("ix_tasks_created_at"), "tasks", ["created_at"], unique=False)

    # WorkerRuns
    op.create_index(
        op.f("ix_worker_runs_worker_type"), "worker_runs", ["worker_type"], unique=False
    )
    op.create_index(
        op.f("ix_worker_runs_finished_at"), "worker_runs", ["finished_at"], unique=False
    )
    op.create_index(op.f("ix_worker_runs_started_at"), "worker_runs", ["started_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_worker_runs_started_at"), table_name="worker_runs")
    op.drop_index(op.f("ix_worker_runs_finished_at"), table_name="worker_runs")
    op.drop_index(op.f("ix_worker_runs_worker_type"), table_name="worker_runs")
    op.drop_index(op.f("ix_tasks_created_at"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_attempt_count"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_status"), table_name="tasks")
