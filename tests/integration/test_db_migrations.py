"""Integration tests for the Alembic migration flow."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

EXPECTED_TABLES = {
    "alembic_version",
    "artifacts",
    "human_interactions",
    "inbound_deliveries",
    "memory_personal",
    "memory_project",
    "sessions",
    "session_states",
    "tasks",
    "task_timeline_events",
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
        "ck_tasks_worker_type": {"gemini", "codex", "openrouter"},
        "ck_tasks_worker_override_type": {"gemini", "codex", "openrouter"},
    },
    "worker_runs": {
        "ck_worker_runs_worker_type": {"gemini", "codex", "openrouter"},
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
            "review_result",
            "independent_review_result",
        },
    },
    "human_interactions": {
        "ck_human_interactions_human_interaction_type": {
            "clarification",
            "permission",
            "review",
            "merge",
            "blocked_help",
        },
        "ck_human_interactions_human_interaction_status": {
            "pending",
            "resolved",
            "rejected",
            "cancelled",
        },
    },
    "task_timeline_events": {
        "ck_task_timeline_events_event_type": {
            "task_ingested",
            "task_classified",
            "task_planned",
            "task_spec_generated",
            "memory_loaded",
            "worker_selected",
            "approval_requested",
            "approval_granted",
            "approval_rejected",
            "worker_dispatched",
            "worker_completed",
            "worker_failed",
            "worker_error",
            "verification_started",
            "verification_completed",
            "task_completed",
            "task_failed",
            "task_cancelled",
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
    assert {
        "task_text",
        "worker_override",
        "constraints",
        "task_spec",
        "budget",
        "chosen_worker",
        "route_reason",
    } <= {column["name"] for column in inspector.get_columns("tasks")}
    assert {
        "session_id",
        "requested_permission",
        "budget_usage",
        "verifier_outcome",
        "commands_run",
        "artifact_index",
        "retention_expires_at",
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

    # Verify unique constraint on task_timeline_events
    timeline_unique_constraints = {
        constraint["name"]: constraint
        for constraint in inspector.get_unique_constraints("task_timeline_events")
    }
    assert "uq_task_timeline_events_task_attempt_seq" in timeline_unique_constraints
    assert timeline_unique_constraints["uq_task_timeline_events_task_attempt_seq"][
        "column_names"
    ] == ["task_id", "attempt_number", "sequence_number"]


def test_alembic_downgrade_cleans_review_result_artifacts(tmp_path: Path) -> None:
    """Downgrading should remove review_result rows before restoring old constraints."""
    database_path = tmp_path / "downgrade_schema.db"
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("script_location", str(Path("db/migrations").resolve()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        now = "2026-04-22T00:00:00+00:00"
        connection.execute(
            text(
                "INSERT INTO users (id, external_user_id, display_name, created_at, updated_at) "
                "VALUES (:id, :external_user_id, :display_name, :created_at, :updated_at)"
            ),
            {
                "id": "u1",
                "external_user_id": "http:test-user",
                "display_name": "Test User",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions "
                "(id, user_id, channel, external_thread_id, active_task_id, status, "
                "last_seen_at, created_at, updated_at) "
                "VALUES (:id, :user_id, :channel, :external_thread_id, :active_task_id, "
                ":status, :last_seen_at, :created_at, :updated_at)"
            ),
            {
                "id": "s1",
                "user_id": "u1",
                "channel": "http",
                "external_thread_id": "thread-1",
                "active_task_id": None,
                "status": "active",
                "last_seen_at": None,
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO tasks "
                "(id, session_id, repo_url, branch, callback_url, task_text, worker_override, "
                "constraints, task_spec, budget, secrets, secrets_encrypted, status, "
                "attempt_count, max_attempts, next_attempt_at, lease_owner, lease_expires_at, "
                "last_error, "
                "priority, chosen_worker, route_reason, created_at, updated_at) "
                "VALUES (:id, :session_id, :repo_url, :branch, :callback_url, :task_text, "
                ":worker_override, :constraints, :task_spec, :budget, :secrets, "
                ":secrets_encrypted, :status, :attempt_count, :max_attempts, "
                ":next_attempt_at, :lease_owner, :lease_expires_at, :last_error, "
                ":priority, :chosen_worker, :route_reason, "
                ":created_at, :updated_at)"
            ),
            {
                "id": "t1",
                "session_id": "s1",
                "repo_url": "https://example.com/repo.git",
                "branch": "master",
                "callback_url": None,
                "task_text": "test",
                "worker_override": None,
                "constraints": "{}",
                "task_spec": None,
                "budget": "{}",
                "secrets": "{}",
                "secrets_encrypted": 0,
                "status": "completed",
                "attempt_count": 0,
                "max_attempts": 3,
                "next_attempt_at": None,
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error": None,
                "priority": 0,
                "chosen_worker": "codex",
                "route_reason": "test",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO worker_runs "
                "(id, task_id, session_id, worker_type, workspace_id, started_at, finished_at, "
                "retention_expires_at, status, summary, requested_permission, budget_usage, "
                "verifier_outcome, commands_run, files_changed_count, files_changed, "
                "artifact_index) "
                "VALUES (:id, :task_id, :session_id, :worker_type, :workspace_id, :started_at, "
                ":finished_at, :retention_expires_at, :status, :summary, "
                ":requested_permission, :budget_usage, :verifier_outcome, :commands_run, "
                ":files_changed_count, :files_changed, :artifact_index)"
            ),
            {
                "id": "r1",
                "task_id": "t1",
                "session_id": "s1",
                "worker_type": "codex",
                "workspace_id": None,
                "started_at": now,
                "finished_at": now,
                "retention_expires_at": None,
                "status": "success",
                "summary": "ok",
                "requested_permission": None,
                "budget_usage": "{}",
                "verifier_outcome": None,
                "commands_run": "[]",
                "files_changed_count": 0,
                "files_changed": "[]",
                "artifact_index": "[]",
            },
        )
        connection.execute(
            text(
                "INSERT INTO artifacts "
                "(id, run_id, artifact_type, name, uri, artifact_metadata, created_at, "
                "updated_at) "
                "VALUES (:id, :run_id, :artifact_type, :name, :uri, :artifact_metadata, "
                ":created_at, :updated_at)"
            ),
            {
                "id": "a1",
                "run_id": "r1",
                "artifact_type": "review_result",
                "name": "review_result",
                "uri": "inline://review_result",
                "artifact_metadata": "{}",
                "created_at": now,
                "updated_at": now,
            },
        )

    command.downgrade(config, "20260422_0016")

    inspector = inspect(engine)
    artifact_constraints = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspector.get_check_constraints("artifacts")
    }
    assert "ck_artifacts_artifact_type" in artifact_constraints
    assert "review_result" not in artifact_constraints["ck_artifacts_artifact_type"]

    with engine.connect() as connection:
        remaining = connection.execute(
            text("SELECT COUNT(*) FROM artifacts WHERE artifact_type = 'review_result'")
        ).scalar_one()
    assert remaining == 0
