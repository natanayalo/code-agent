"""Integration tests for the Alembic migration flow."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

EXPECTED_TABLES = {
    "alembic_version",
    "artifacts",
    "memory_personal",
    "memory_project",
    "sessions",
    "tasks",
    "users",
    "worker_runs",
}


def test_alembic_upgrade_creates_expected_tables(tmp_path: Path) -> None:
    """Upgrading to head creates the initial persistence schema."""
    database_path = tmp_path / "schema.db"
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("script_location", str(Path("db/migrations").resolve()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)

    assert EXPECTED_TABLES == set(inspector.get_table_names())
    assert {"channel", "external_thread_id", "status"} <= {
        column["name"] for column in inspector.get_columns("sessions")
    }
    assert {"task_text", "chosen_worker", "route_reason"} <= {
        column["name"] for column in inspector.get_columns("tasks")
    }
    assert {"commands_run", "artifact_index", "files_changed_count"} <= {
        column["name"] for column in inspector.get_columns("worker_runs")
    }
