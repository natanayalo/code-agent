"""Integration tests for the Alembic migration flow."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

EXPECTED_TABLES = {
    "alembic_version",
    "artifacts",
    "execution_plans",
    "execution_plan_nodes",
    "human_interactions",
    "inbound_deliveries",
    "memory_personal",
    "memory_proposals",
    "memory_project",
    "proposals",
    "sessions",
    "session_states",
    "tasks",
    "task_timeline_events",
    "users",
    "worker_nodes",
    "worker_runs",
}

EXPECTED_CHECK_CONSTRAINTS = {
    "sessions": {
        "ck_sessions_session_status": {"active", "closed"},
    },
    "proposals": {
        "ck_proposals_proposal_status": {
            "pending_review",
            "accepted",
            "rejected",
            "implemented",
        },
        "ck_proposals_proposal_type": {
            "scout",
            "reflection",
        },
    },
    "memory_proposals": {
        "ck_memory_proposals_memory_proposal_category": {
            "personal",
            "project",
        },
        "ck_memory_proposals_memory_proposal_status": {
            "pending_review",
            "accepted",
            "rejected",
        },
        "ck_memory_proposals_category_repo_url": {
            "category = 'project'",
            "repo_url IS NOT NULL",
            "category = 'personal'",
            "repo_url IS NULL",
        },
        "ck_memory_proposals_confidence_range": {
            "confidence >= 0.0",
            "confidence <= 1.0",
        },
    },
    "execution_plan_nodes": {
        "ck_execution_plan_nodes_execution_plan_node_status": {
            "pending",
            "active",
            "blocked",
            "completed",
            "failed",
            "skipped",
        },
    },
    "tasks": {
        "ck_tasks_task_status": {
            "pending",
            "in_progress",
            "completed",
            "failed",
            "cancelled",
        },
        "ck_tasks_worker_type": {"antigravity", "codex", "openrouter"},
        "ck_tasks_worker_override_type": {"antigravity", "codex", "openrouter"},
    },
    "worker_runs": {
        "ck_worker_runs_worker_type": {"antigravity", "codex", "openrouter"},
        "ck_worker_runs_worker_run_status": {
            "queued",
            "running",
            "success",
            "failure",
            "error",
            "cancelled",
        },
    },
    "worker_nodes": {
        "ck_worker_nodes_worker_type": {"antigravity", "codex", "openrouter"},
        "ck_worker_nodes_worker_node_status": {
            "active",
            "draining",
            "offline",
            "quarantined",
        },
        "ck_worker_nodes_worker_capacity_positive": {"capacity > 0"},
        "ck_worker_nodes_worker_load_nonnegative": {"current_load >= 0"},
        "ck_worker_nodes_worker_load_within_capacity": {"current_load <= capacity"},
        "ck_worker_nodes_worker_failures_nonnegative": {"consecutive_failures >= 0"},
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
            "task_spec_and_route_generated",
            "memory_loaded",
            "memory_persisted",
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
            "verification_skipped",
            "task_completed",
            "task_failed",
            "task_cancelled",
            "workspace_provisioned",
            "environment_initialized",
            "infra_failure",
            "delivery_started",
            "delivery_completed",
            "delivery_failed",
        },
    },
}


def _column_names(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _assert_upgrade_columns(inspector) -> None:
    assert {"channel", "external_thread_id", "status"} <= _column_names(inspector, "sessions")
    assert {
        "task_text",
        "worker_override",
        "constraints",
        "task_spec",
        "budget",
        "chosen_worker",
        "route_reason",
    } <= _column_names(inspector, "tasks")
    assert {
        "session_id",
        "requested_permission",
        "budget_usage",
        "verifier_outcome",
        "commands_run",
        "artifact_index",
        "runtime_manifest",
        "retention_expires_at",
        "files_changed_count",
    } <= _column_names(inspector, "worker_runs")
    assert {
        "worker_id",
        "worker_type",
        "status",
        "supported_profiles",
        "capabilities",
        "last_heartbeat_at",
        "capacity",
        "current_load",
        "consecutive_failures",
        "quarantine_reason",
    } <= _column_names(inspector, "worker_nodes")


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
    _assert_upgrade_columns(inspector)
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
    assert {
        "category",
        "repo_url",
        "memory_key",
        "value",
        "source",
        "confidence",
        "scope",
        "requires_verification",
        "status",
        "title",
        "summary",
        "evidence",
        "task_id",
        "session_id",
        "accepted_memory_id",
        "reviewed_at",
    } <= _column_names(inspector, "memory_proposals")

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


def test_sqlite_upgrade_skips_memory_fulltext_columns(tmp_path: Path) -> None:
    """The Postgres-only search migration should remain a no-op on SQLite."""
    database_path = tmp_path / "memory_search_sqlite.db"
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("script_location", str(Path("db/migrations").resolve()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)

    assert "search_vector" not in _column_names(inspector, "memory_personal")
    assert "search_vector" not in _column_names(inspector, "memory_project")


def test_personal_memory_scope_migration_deduplicates_and_removes_user_id(
    tmp_path: Path,
) -> None:
    """The operator-global personal memory migration should keep newest duplicate keys."""
    database_path = tmp_path / "personal_memory_global.db"
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("script_location", str(Path("db/migrations").resolve()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "20260703_0029")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        old_time = "2026-07-02T00:00:00+00:00"
        new_time = "2026-07-03T00:00:00+00:00"
        connection.execute(
            text(
                "INSERT INTO users (id, external_user_id, display_name, created_at, updated_at) "
                "VALUES "
                "('u-old', 'old-user', 'Old User', :old_time, :old_time), "
                "('u-new', 'new-user', 'New User', :old_time, :old_time)"
            ),
            {"old_time": old_time},
        )
        connection.execute(
            text(
                "INSERT INTO memory_personal "
                "(id, user_id, memory_key, value, source, confidence, scope, "
                "last_verified_at, requires_verification, created_at, updated_at) "
                "VALUES "
                "('pm-old', 'u-old', 'style', '{}', NULL, 1.0, NULL, NULL, 1, "
                ":old_time, :old_time), "
                "('pm-new', 'u-new', 'style', '{}', NULL, 1.0, NULL, NULL, 0, "
                ":old_time, :new_time)"
            ),
            {"old_time": old_time, "new_time": new_time},
        )

    command.upgrade(config, "head")

    inspector = inspect(engine)
    assert "user_id" not in _column_names(inspector, "memory_personal")
    unique_constraints = {
        constraint["name"]: constraint
        for constraint in inspector.get_unique_constraints("memory_personal")
    }
    assert unique_constraints["uq_memory_personal_key"]["column_names"] == ["memory_key"]

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, memory_key, requires_verification FROM memory_personal")
        ).all()

    assert rows == [("pm-new", "style", 0)]


def test_personal_memory_scope_downgrade_assigns_operator_user(tmp_path: Path) -> None:
    """Downgrade should restore user ownership with a fallback operator user."""
    database_path = tmp_path / "personal_memory_global_downgrade.db"
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("script_location", str(Path("db/migrations").resolve()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    now = "2026-07-03T00:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO memory_personal "
                "(id, memory_key, value, source, confidence, scope, last_verified_at, "
                "requires_verification, created_at, updated_at) "
                "VALUES ('pm-global', 'style', '{}', NULL, 1.0, NULL, NULL, 1, "
                ":now, :now)"
            ),
            {"now": now},
        )

    command.downgrade(config, "20260703_0029")

    inspector = inspect(engine)
    assert "user_id" in _column_names(inspector, "memory_personal")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT m.user_id, u.external_user_id "
                "FROM memory_personal m "
                "JOIN users u ON u.id = m.user_id "
                "WHERE m.id = 'pm-global'"
            )
        ).one()

    assert row == ("operator-personal-memory-user", "operator:personal-memory")


def _seed_downgrade_users_and_sessions(connection, now: str) -> None:
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


def _seed_downgrade_tasks(connection, now: str) -> None:
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


def _seed_downgrade_runs_and_artifacts(connection, now: str) -> None:
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


def _seed_antigravity_migration_task(connection, now: str) -> None:
    connection.execute(
        text(
            "INSERT INTO tasks "
            "(id, session_id, repo_url, branch, callback_url, task_text, worker_override, "
            "constraints, task_spec, budget, secrets, secrets_encrypted, status, "
            "attempt_count, max_attempts, next_attempt_at, lease_owner, lease_expires_at, "
            "last_error, priority, chosen_worker, chosen_profile, route_reason, "
            "created_at, updated_at) "
            "VALUES (:id, :session_id, :repo_url, :branch, :callback_url, :task_text, "
            ":worker_override, :constraints, :task_spec, :budget, :secrets, "
            ":secrets_encrypted, :status, :attempt_count, :max_attempts, "
            ":next_attempt_at, :lease_owner, :lease_expires_at, :last_error, "
            ":priority, :chosen_worker, :chosen_profile, :route_reason, "
            ":created_at, :updated_at)"
        ),
        {
            "id": "t-antigravity",
            "session_id": "s1",
            "repo_url": "https://example.com/repo.git",
            "branch": "master",
            "callback_url": None,
            "task_text": "test",
            "worker_override": "gemini",
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
            "chosen_worker": "gemini",
            "chosen_profile": "gemini-native-executor",
            "route_reason": "test",
            "created_at": now,
            "updated_at": now,
        },
    )


def _seed_antigravity_migration_run(connection, now: str) -> None:
    connection.execute(
        text(
            "INSERT INTO worker_runs "
            "(id, task_id, session_id, worker_type, workspace_id, started_at, finished_at, "
            "retention_expires_at, status, worker_profile, summary, requested_permission, "
            "budget_usage, verifier_outcome, commands_run, files_changed_count, "
            "files_changed, artifact_index) "
            "VALUES (:id, :task_id, :session_id, :worker_type, :workspace_id, :started_at, "
            ":finished_at, :retention_expires_at, :status, :worker_profile, :summary, "
            ":requested_permission, :budget_usage, :verifier_outcome, :commands_run, "
            ":files_changed_count, :files_changed, :artifact_index)"
        ),
        {
            "id": "r-antigravity",
            "task_id": "t-antigravity",
            "session_id": "s1",
            "worker_type": "gemini",
            "workspace_id": None,
            "started_at": now,
            "finished_at": now,
            "retention_expires_at": None,
            "status": "success",
            "worker_profile": "gemini-native-executor",
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


def test_antigravity_worker_type_migration_updates_existing_gemini_rows(
    tmp_path: Path,
) -> None:
    """Upgrading T-205 should rewrite persisted Gemini worker identifiers."""

    database_path = tmp_path / "antigravity_worker_type.db"
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("script_location", str(Path("db/migrations").resolve()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "a44580010250")

    engine = create_engine(f"sqlite:///{database_path}")
    now = "2026-06-19T00:00:00+00:00"
    with engine.begin() as connection:
        _seed_downgrade_users_and_sessions(connection, now)
        _seed_antigravity_migration_task(connection, now)
        _seed_antigravity_migration_run(connection, now)

    command.upgrade(config, "head")

    with engine.connect() as connection:
        task_row = connection.execute(
            text(
                "SELECT chosen_worker, worker_override, chosen_profile FROM tasks "
                "WHERE id = 't-antigravity'"
            )
        ).one()
        run_row = connection.execute(
            text("SELECT worker_type, worker_profile FROM worker_runs WHERE id = 'r-antigravity'")
        ).one()
    assert task_row == ("antigravity", "antigravity", "antigravity-native-executor")
    assert run_row == ("antigravity", "antigravity-native-executor")

    inspector = inspect(engine)
    task_constraints = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspector.get_check_constraints("tasks")
    }
    assert "antigravity" in task_constraints["ck_tasks_worker_type"]
    assert "gemini" not in task_constraints["ck_tasks_worker_type"]


def test_memory_persisted_timeline_event_can_be_written_after_upgrade(tmp_path: Path) -> None:
    """The head migration should permit persisted memory timeline evidence."""
    database_path = tmp_path / "memory_persisted_timeline.db"
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("script_location", str(Path("db/migrations").resolve()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    now = "2026-07-02T00:00:00+00:00"
    with engine.begin() as connection:
        _seed_downgrade_users_and_sessions(connection, now)
        _seed_downgrade_tasks(connection, now)
        connection.execute(
            text(
                "INSERT INTO task_timeline_events "
                "(id, task_id, attempt_number, sequence_number, event_type, payload, "
                "message, created_at, updated_at) "
                "VALUES (:id, :task_id, :attempt_number, :sequence_number, :event_type, "
                ":payload, :message, :created_at, :updated_at)"
            ),
            {
                "id": "evt-memory-persisted",
                "task_id": "t1",
                "attempt_number": 0,
                "sequence_number": 0,
                "event_type": "memory_persisted",
                "payload": "{}",
                "message": "Persisted 1 memory entry.",
                "created_at": now,
                "updated_at": now,
            },
        )
        count = connection.execute(
            text(
                "SELECT COUNT(*) FROM task_timeline_events " "WHERE event_type = 'memory_persisted'"
            )
        ).scalar_one()

    assert count == 1


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
        _seed_downgrade_users_and_sessions(connection, now)
        _seed_downgrade_tasks(connection, now)
        _seed_downgrade_runs_and_artifacts(connection, now)

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
