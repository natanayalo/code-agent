"""Integration tests for context_envelope artifact_type Alembic migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


@pytest.fixture
def alembic_sqlite_engine(tmp_path: Path) -> tuple[Any, Config]:
    db_path = tmp_path / "migration_test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    repo_root = Path(__file__).resolve().parents[2]
    alembic_ini_path = repo_root / "alembic.ini"

    config = Config(str(alembic_ini_path))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    config.set_main_option("script_location", str(repo_root / "db" / "migrations"))

    return engine, config


def test_context_envelope_migration_upgrade_and_downgrade(alembic_sqlite_engine: Any) -> None:
    engine, config = alembic_sqlite_engine

    # 1. Upgrade to latest head (20260913_0051)
    command.upgrade(config, "head")

    now = "2026-09-13 22:00:00"
    user_id = f"user-{uuid4().hex[:8]}"
    session_id = f"sess-{uuid4().hex[:8]}"
    task_id = f"task-{uuid4().hex[:8]}"
    run_id = f"run-{uuid4().hex[:8]}"
    art_id = f"art-{uuid4().hex[:8]}"

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, external_user_id, display_name, created_at, updated_at) "
                "VALUES (:id, 'user_ext_1', 'Operator', :now, :now)"
            ),
            {"id": user_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO sessions "
                "(id, user_id, channel, external_thread_id, status, created_at, updated_at) "
                "VALUES (:id, :user_id, 'web', 'thread-1', 'active', :now, :now)"
            ),
            {"id": session_id, "user_id": user_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO tasks "
                "(id, session_id, task_text, repo_url, branch, status, priority, "
                "created_at, updated_at) "
                "VALUES (:id, :session_id, 'Test task', 'https://example.com', 'main', "
                "'completed', 0, :now, :now)"
            ),
            {"id": task_id, "session_id": session_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO worker_runs "
                "(id, task_id, session_id, worker_type, workspace_id, started_at, finished_at, "
                "retention_expires_at, status, summary, requested_permission, budget_usage, "
                "verifier_outcome, commands_run, files_changed_count, files_changed, "
                "artifact_index) "
                "VALUES (:id, :task_id, :session_id, 'antigravity', NULL, :now, :now, "
                "NULL, 'success', 'Done', NULL, '{}', NULL, '[]', 0, '[]', '[]')"
            ),
            {"id": run_id, "task_id": task_id, "session_id": session_id, "now": now},
        )
        # 2. Insert context_envelope artifact
        connection.execute(
            text(
                "INSERT INTO artifacts "
                "(id, run_id, artifact_type, name, uri, artifact_metadata, "
                "created_at, updated_at) "
                "VALUES (:id, :run_id, 'context_envelope', 'context_envelope', "
                "'envelope://123', '{\"test\": true}', :now, :now)"
            ),
            {"id": art_id, "run_id": run_id, "now": now},
        )

        row = connection.execute(
            text("SELECT artifact_type, uri FROM artifacts WHERE id = :id"),
            {"id": art_id},
        ).fetchone()
        assert row is not None
        assert row[0] == "context_envelope"

    # 3. Downgrade to previous revision (20260817_0050)
    command.downgrade(config, "20260817_0050")

    # 4. Verify context_envelope artifact was cleaned up
    with engine.connect() as connection:
        remaining = connection.execute(
            text("SELECT COUNT(*) FROM artifacts WHERE artifact_type = 'context_envelope'")
        ).scalar_one()
        assert remaining == 0
