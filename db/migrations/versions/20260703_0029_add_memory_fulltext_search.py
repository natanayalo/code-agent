"""add memory full-text search columns

Revision ID: 20260703_0029
Revises: 20260702_0028
Create Date: 2026-07-03 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "20260703_0029"
down_revision = "20260702_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add generated tsvector columns and GIN indexes for skeptical memory search."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        """
        ALTER TABLE memory_personal
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector(
                'english',
                coalesce(memory_key, '') || ' ' || coalesce(CAST(value AS TEXT), '')
            )
        ) STORED
        """
    )
    op.execute(
        """
        CREATE INDEX ix_memory_personal_search_vector
        ON memory_personal USING GIN (search_vector)
        """
    )

    op.execute(
        """
        ALTER TABLE memory_project
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector(
                'english',
                coalesce(memory_key, '') || ' ' || coalesce(CAST(value AS TEXT), '')
            )
        ) STORED
        """
    )
    op.execute(
        """
        CREATE INDEX ix_memory_project_search_vector
        ON memory_project USING GIN (search_vector)
        """
    )


def downgrade() -> None:
    """Remove generated skeptical-memory search columns and indexes."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.drop_index("ix_memory_project_search_vector", table_name="memory_project")
    op.execute("ALTER TABLE memory_project DROP COLUMN search_vector")
    op.drop_index("ix_memory_personal_search_vector", table_name="memory_personal")
    op.execute("ALTER TABLE memory_personal DROP COLUMN search_vector")
