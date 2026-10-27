"""Migration upgrade and write-path coverage for readiness alert persistence."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_readiness_alert_migration_upgrades_and_persists_transitions(tmp_path: Path) -> None:
    """The new tables accept durable state and transition-delivery records."""
    db_path = tmp_path / "readiness_alerts.db"
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("script_location", str(Path("db/migrations").resolve()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "readiness_alert_states" in inspector.get_table_names()
    assert "readiness_alert_deliveries" in inspector.get_table_names()
    assert inspector.get_unique_constraints("readiness_alert_deliveries")[0]["column_names"] == [
        "reason",
        "transition_number",
    ]

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO readiness_alert_states "
                "(reason, degraded, transition_number, last_transition_at, last_checked_at, "
                "created_at, updated_at) VALUES "
                "('temporal_unavailable', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO readiness_alert_deliveries "
                "(id, reason, transition_number, transition, status, attempts, next_attempt_at, "
                "created_at, updated_at) VALUES "
                "('delivery-1', 'temporal_unavailable', 1, 'degraded', 'pending', 0, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM readiness_alert_deliveries WHERE status='pending'")
            ).scalar_one()
            == 1
        )
