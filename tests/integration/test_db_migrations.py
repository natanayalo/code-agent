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
    "session_states",
    "tasks",
    "users",
    "worker_runs",
}

EXPECTED_CHECK_CONSTRAINTS = {
    "sessions": {
        "ck_sessions_session_status": {"active", "closed"},
    },
    "tasks": {
        "ck_tasks_task_status": {
            "pending",
            "in_progress",
            "completed",
            "failed",
            "cancelled",
        },
        "ck_tasks_worker_type": {"claude", "codex"},
    },
    "worker_runs": {
        "ck_worker_runs_worker_type": {"claude", "codex"},
        "ck_worker_runs_worker_run_status": {
            "queued",
            "running",
            "success",
            "failure",
            "error",
            "cancelled",
        },
    },
    "artifacts": {
        "ck_artifacts_artifact_type": {
            "log",
            "diff",
            "test_report",
            "result_summary",
            "workspace",
        },
    },
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
    assert {
        "session_id",
        "requested_permission",
        "budget_usage",
        "verifier_outcome",
        "commands_run",
        "artifact_index",
        "files_changed_count",
    } <= {column["name"] for column in inspector.get_columns("worker_runs")}
    worker_run_foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys("worker_runs")
    }
    assert worker_run_foreign_keys["fk_worker_runs_session_id_sessions"]["options"] == {
        "ondelete": "CASCADE"
    }
    session_state_columns = {
        column["name"]: column for column in inspector.get_columns("session_states")
    }
    assert session_state_columns["decisions_made"]["default"] == "'{}'"
    assert session_state_columns["identified_risks"]["default"] == "'{}'"
    assert session_state_columns["files_touched"]["default"] == "'[]'"

    for table_name, expected_constraints in EXPECTED_CHECK_CONSTRAINTS.items():
        actual_constraints = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints(table_name)
        }
        for constraint_name, expected_values in expected_constraints.items():
            assert constraint_name in actual_constraints
            for expected_value in expected_values:
                assert expected_value in actual_constraints[constraint_name]
