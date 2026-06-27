"""add decision_key and hitl_mode to human_interactions"""

revision = "40fcaf6dbdad"
down_revision = "e0cffe741eb4"
branch_labels = None
depends_on = None


import sqlalchemy as sa  # noqa: E402
from alembic import op  # noqa: E402


def upgrade() -> None:
    # Add the decision_key column
    op.add_column(
        "human_interactions", sa.Column("decision_key", sa.String(length=255), nullable=True)
    )
    op.create_index(
        op.f("ix_human_interactions_decision_key"),
        "human_interactions",
        ["decision_key"],
        unique=False,
    )

    # Create the non-native enum constraint manually, then add the column
    hitl_mode_enum = sa.Enum(
        "require_approval",
        "proceed_with_flag",
        "notify_only",
        name="human_interaction_hitl_mode",
        native_enum=False,
        create_constraint=True,
    )
    op.add_column(
        "human_interactions",
        sa.Column("hitl_mode", hitl_mode_enum, nullable=False, server_default="require_approval"),
    )


def downgrade() -> None:
    op.drop_column("human_interactions", "hitl_mode")
    op.drop_index(op.f("ix_human_interactions_decision_key"), table_name="human_interactions")
    op.drop_column("human_interactions", "decision_key")
