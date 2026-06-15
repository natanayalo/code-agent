"""add proposal_type to proposals"""

revision = "a44580010250"
down_revision = "c0ffee123456"
branch_labels = None
depends_on = None

from collections.abc import Iterable  # noqa: E402

import sqlalchemy as sa  # noqa: E402
from alembic import op  # noqa: E402


def _check_condition(column_name: str, values: Iterable[str]) -> str:
    rendered_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({rendered_values})"


def upgrade() -> None:
    op.add_column(
        "proposals",
        sa.Column(
            "proposal_type",
            sa.Enum(
                "scout",
                "reflection",
                name="proposal_type",
                native_enum=False,
                create_constraint=False,
            ),
            server_default="scout",
            nullable=False,
        ),
    )

    with op.batch_alter_table("proposals") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_proposals_proposal_type"), ["proposal_type"], unique=False
        )
        batch_op.create_check_constraint(
            op.f("ck_proposals_proposal_type"),
            _check_condition("proposal_type", ["scout", "reflection"]),
        )


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.drop_constraint(op.f("ck_proposals_proposal_type"), type_="check")
        batch_op.drop_index(batch_op.f("ix_proposals_proposal_type"))
        batch_op.drop_column("proposal_type")
