"""collapse personal memory to operator-global scope

Revision ID: 20260703_0030
Revises: 20260703_0029
Create Date: 2026-07-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260703_0030"
down_revision = "20260703_0029"
branch_labels = None
depends_on = None

_OPERATOR_MEMORY_USER_ID = "operator-personal-memory-user"
_OPERATOR_MEMORY_EXTERNAL_ID = "operator:personal-memory"


def upgrade() -> None:
    """Remove user partitioning from personal memory."""
    op.execute(
        """
        DELETE FROM memory_personal
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY memory_key
                        ORDER BY updated_at DESC, created_at DESC, id DESC
                    ) AS duplicate_rank
                FROM memory_personal
            ) ranked
            WHERE duplicate_rank > 1
        )
        """
    )

    with op.batch_alter_table("memory_personal", schema=None) as batch_op:
        batch_op.drop_constraint("uq_memory_personal_user_key", type_="unique")
        batch_op.drop_constraint("fk_memory_personal_user_id_users", type_="foreignkey")
        batch_op.drop_index("ix_memory_personal_user_id")
        batch_op.drop_column("user_id")
        batch_op.create_unique_constraint("uq_memory_personal_key", ["memory_key"])


def downgrade() -> None:
    """Restore user partitioning by assigning personal memory to a fallback operator user."""
    with op.batch_alter_table("memory_personal", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))

    op.execute(
        f"""
        INSERT INTO users (id, external_user_id, display_name, created_at, updated_at)
        SELECT
            '{_OPERATOR_MEMORY_USER_ID}',
            '{_OPERATOR_MEMORY_EXTERNAL_ID}',
            'Operator Personal Memory',
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        WHERE NOT EXISTS (
            SELECT 1
            FROM users
            WHERE id = '{_OPERATOR_MEMORY_USER_ID}'
               OR external_user_id = '{_OPERATOR_MEMORY_EXTERNAL_ID}'
        )
        """
    )
    op.execute(
        f"""
        UPDATE memory_personal
        SET user_id = COALESCE(
            (
                SELECT id
                FROM users
                WHERE external_user_id = '{_OPERATOR_MEMORY_EXTERNAL_ID}'
                LIMIT 1
            ),
            '{_OPERATOR_MEMORY_USER_ID}'
        )
        WHERE user_id IS NULL
        """
    )

    with op.batch_alter_table("memory_personal", schema=None) as batch_op:
        batch_op.drop_constraint("uq_memory_personal_key", type_="unique")
        batch_op.alter_column(
            "user_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_memory_personal_user_id_users",
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_memory_personal_user_id", ["user_id"])
        batch_op.create_unique_constraint(
            "uq_memory_personal_user_key",
            ["user_id", "memory_key"],
        )
