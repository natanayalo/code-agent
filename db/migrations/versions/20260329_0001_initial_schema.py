"""Create the initial persistence schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260329_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the initial T-010 tables."""
    op.create_table(
        "users",
        sa.Column("external_user_id", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("external_user_id", name=op.f("uq_users_external_user_id")),
    )
    op.create_table(
        "memory_project",
        sa.Column("repo_url", sa.String(length=512), nullable=False),
        sa.Column("memory_key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_project")),
        sa.UniqueConstraint("repo_url", "memory_key", name="uq_memory_project_repo_key"),
    )
    op.create_index(
        op.f("ix_memory_project_repo_url"),
        "memory_project",
        ["repo_url"],
        unique=False,
    )
    op.create_table(
        "sessions",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("external_thread_id", sa.String(length=255), nullable=False),
        sa.Column("active_task_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint(
            "channel",
            "external_thread_id",
            name="uq_sessions_channel_external_thread_id",
        ),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.create_table(
        "memory_personal",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("memory_key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_memory_personal_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_personal")),
        sa.UniqueConstraint("user_id", "memory_key", name="uq_memory_personal_user_key"),
    )
    op.create_index(
        op.f("ix_memory_personal_user_id"),
        "memory_personal",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "tasks",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("repo_url", sa.String(length=512), nullable=True),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("task_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("chosen_worker", sa.String(length=50), nullable=True),
        sa.Column("route_reason", sa.String(length=255), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_tasks_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
    )
    op.create_index(op.f("ix_tasks_session_id"), "tasks", ["session_id"], unique=False)
    op.create_table(
        "worker_runs",
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("worker_type", sa.String(length=50), nullable=False),
        sa.Column("workspace_id", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("commands_run", sa.JSON(), nullable=True),
        sa.Column("files_changed_count", sa.Integer(), nullable=False),
        sa.Column("artifact_index", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_worker_runs_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_runs")),
    )
    op.create_index(
        op.f("ix_worker_runs_task_id"),
        "worker_runs",
        ["task_id"],
        unique=False,
    )
    op.create_table(
        "artifacts",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_type", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("uri", sa.String(length=1024), nullable=False),
        sa.Column("artifact_metadata", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["worker_runs.id"],
            name=op.f("fk_artifacts_run_id_worker_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifacts")),
    )
    op.create_index(op.f("ix_artifacts_run_id"), "artifacts", ["run_id"], unique=False)


def downgrade() -> None:
    """Drop the initial T-010 tables."""
    op.drop_index(op.f("ix_artifacts_run_id"), table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_index(op.f("ix_worker_runs_task_id"), table_name="worker_runs")
    op.drop_table("worker_runs")
    op.drop_index(op.f("ix_tasks_session_id"), table_name="tasks")
    op.drop_table("tasks")
    op.drop_index(op.f("ix_memory_personal_user_id"), table_name="memory_personal")
    op.drop_table("memory_personal")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_index(op.f("ix_memory_project_repo_url"), table_name="memory_project")
    op.drop_table("memory_project")
    op.drop_table("users")
