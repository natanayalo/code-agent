"""add memory observations

Revision ID: 20260704_0033
Revises: 20260704_0032
Create Date: 2026-07-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260704_0033"
down_revision = "20260704_0032"
branch_labels = None
depends_on = None


def _create_observations_table() -> None:
    op.create_table(
        "memory_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("repo_url", sa.String(length=512), nullable=True),
        sa.Column("worker_type", sa.String(length=50), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("privacy_stripped", sa.Boolean(), nullable=False),
        sa.Column(
            "admission_status",
            sa.String(length=50),
            nullable=False,
            server_default="not_required",
        ),
        sa.Column("admission_processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admission_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "admission_status IN ('not_required', 'pending', 'processed', 'invalid', 'failed')",
            name="memory_observation_admission_status",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_memory_observations_session_id_sessions",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_memory_observations_task_id_tasks",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_observations_task_id",
        "memory_observations",
        ["task_id"],
    )
    op.create_index(
        "ix_memory_observations_session_id",
        "memory_observations",
        ["session_id"],
    )
    op.create_index(
        "ix_memory_observations_repo_url",
        "memory_observations",
        ["repo_url"],
    )
    op.create_index(
        "ix_memory_observations_admission_status",
        "memory_observations",
        ["admission_status"],
    )


def _setup_postgres_search(bind: Connection) -> None:
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            ALTER TABLE memory_observations
            ADD COLUMN search_vector tsvector
            GENERATED ALWAYS AS (
                to_tsvector(
                    'english',
                    coalesce(summary, '') || ' ' || coalesce(content, '')
                )
            ) STORED
            """
        )
        op.execute(
            """
            CREATE INDEX ix_memory_observations_search_vector
            ON memory_observations USING GIN (search_vector)
            """
        )


def _add_source_observation_columns() -> None:
    op.add_column(
        "memory_admission_decisions",
        sa.Column("source_observation_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "memory_proposals",
        sa.Column("source_observation_id", sa.String(length=36), nullable=True),
    )


def _create_partial_unique_indexes() -> None:
    op.create_index(
        "uq_idx_decision_source_observation_id",
        "memory_admission_decisions",
        ["source_observation_id"],
        unique=True,
        postgresql_where=sa.text("source_observation_id IS NOT NULL"),
        sqlite_where=sa.text("source_observation_id IS NOT NULL"),
    )
    op.create_index(
        "uq_idx_proposal_source_observation_id",
        "memory_proposals",
        ["source_observation_id"],
        unique=True,
        postgresql_where=sa.text("source_observation_id IS NOT NULL"),
        sqlite_where=sa.text("source_observation_id IS NOT NULL"),
    )


def upgrade() -> None:
    """Create memory_observations table, add columns, and define indexes."""
    _create_observations_table()
    _setup_postgres_search(op.get_bind())
    _add_source_observation_columns()
    _create_partial_unique_indexes()


def downgrade() -> None:
    """Drop indexes, columns, and memory_observations table."""
    # 1. Drop partial unique indexes first before dropping columns
    op.drop_index(
        "uq_idx_proposal_source_observation_id",
        table_name="memory_proposals",
        postgresql_where=sa.text("source_observation_id IS NOT NULL"),
        sqlite_where=sa.text("source_observation_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_idx_decision_source_observation_id",
        table_name="memory_admission_decisions",
        postgresql_where=sa.text("source_observation_id IS NOT NULL"),
        sqlite_where=sa.text("source_observation_id IS NOT NULL"),
    )

    # 2. Drop added columns
    op.drop_column("memory_proposals", "source_observation_id")
    op.drop_column("memory_admission_decisions", "source_observation_id")

    # 3. If Postgres, drop search_vector and its GIN index
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_index(
            "ix_memory_observations_search_vector",
            table_name="memory_observations",
        )
        op.drop_column("memory_observations", "search_vector")

    # 4. Drop table and regular indexes
    op.drop_index(
        "ix_memory_observations_admission_status",
        table_name="memory_observations",
    )
    op.drop_index(
        "ix_memory_observations_repo_url",
        table_name="memory_observations",
    )
    op.drop_index(
        "ix_memory_observations_session_id",
        table_name="memory_observations",
    )
    op.drop_index(
        "ix_memory_observations_task_id",
        table_name="memory_observations",
    )
    op.drop_table("memory_observations")
