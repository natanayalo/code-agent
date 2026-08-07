"""Unit tests for CIPollingScheduler in apps/api/ci_polling.py."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.pool import StaticPool

from apps.api.ci_polling import CIPollingScheduler
from apps.api.config import SystemConfig
from db.base import Base
from db.enums import WorkerRunStatus
from db.models import Session as ConversationSession
from db.models import Task, User, WorkerRun
from repositories import create_engine_from_url, create_session_factory


@pytest.fixture
def session_factory():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def sample_user_and_session(session_factory):
    with session_factory() as session:
        user = User(external_user_id="usr-1", display_name="Test User")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="telegram", external_thread_id="th-1")
        session.add(conv)
        session.commit()
        return user, conv


def test_ci_polling_scheduler_start_stop(monkeypatch):
    task_service = MagicMock()
    config_disabled = SystemConfig(
        default_image="img", workspace_root="/tmp", ci_polling_enabled=False
    )
    scheduler = CIPollingScheduler(task_service, config_disabled)

    # Disabled
    scheduler.start()
    assert scheduler._running is False

    # Enabled but no session_factory
    config_enabled = SystemConfig(
        default_image="img", workspace_root="/tmp", ci_polling_enabled=True
    )
    scheduler_enabled = CIPollingScheduler(task_service, config_enabled)
    scheduler_enabled.start()
    assert scheduler_enabled._running is False

    # session_factory set but no event loop
    scheduler_enabled.session_factory = MagicMock()
    scheduler_enabled.start()
    assert scheduler_enabled._running is False

    # In async loop
    async def run_test():
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/gh")
        monkeypatch.setenv("GH_TOKEN", "dummy_token")  # gitleaks:allow

        scheduler_enabled.start()
        assert scheduler_enabled._running is True
        await scheduler_enabled.stop()
        assert scheduler_enabled._running is False
        assert scheduler_enabled._task is None

    asyncio.run(run_test())


def test_get_pending_runs(session_factory, sample_user_and_session):
    user, conv = sample_user_and_session
    task_service = MagicMock()
    task_service.session_factory = session_factory
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)

    with session_factory() as session:
        t = Task(session_id=conv.id, task_text="Task text", repo_url="https://github.com/org/repo")
        session.add(t)
        session.flush()

        run = WorkerRun(
            task_id=t.id,
            session_id=conv.id,
            worker_type="codex",
            started_at=datetime.now(UTC) - timedelta(hours=1),
            status=WorkerRunStatus.SUCCESS,
            delivery_metadata={
                "delivery_mode": "draft_pr",
                "branch_name": "feat-1",
                "ci_status": None,
            },
        )
        session.add(run)
        session.commit()

    pending = scheduler._get_pending_runs()
    assert len(pending) == 1
    assert pending[0]["delivery_metadata"]["branch_name"] == "feat-1"


def test_get_pr_checks():
    task_service = MagicMock()
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)

    with patch("subprocess.run") as mock_run:
        # Success
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps([{"name": "test", "state": "SUCCESS", "link": "http://link"}]),
        )
        checks = scheduler._get_pr_checks("branch-1", "org/repo", "r1", {})
        assert len(checks) == 1

        # Error - not found
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stderr="no pull requests found"
        )
        with patch.object(scheduler, "_update_run_ci_metadata") as mock_update:
            assert scheduler._get_pr_checks("branch-1", "org/repo", "r1", {}) is None
            mock_update.assert_called_once_with("r1", "not_found", [])


def test_poll_run():
    task_service = MagicMock()
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)

    run_info = {
        "run_id": "r1",
        "task_id": "t1",
        "repo_url": "https://github.com/org/repo",
        "delivery_metadata": {"branch_name": "feat-1", "head_sha": "abc1234"},
        "secrets": {"GH_TOKEN": "dummy_token"},  # gitleaks:allow
    }

    with (
        patch.object(scheduler, "_get_pr_checks") as mock_checks,
        patch.object(scheduler, "_update_run_ci_metadata") as mock_update,
        patch.object(scheduler, "_submit_repair_task") as mock_repair,
    ):
        mock_checks.return_value = [{"name": "build", "state": "FAILURE", "link": "http://build"}]
        scheduler._poll_run(run_info)

        mock_update.assert_called_once_with(
            "r1", "failed", [{"name": "build", "state": "FAILURE", "link": "http://build"}]
        )
        mock_repair.assert_called_once()


def test_update_run_ci_metadata(session_factory, sample_user_and_session):
    user, conv = sample_user_and_session
    task_service = MagicMock()
    task_service.session_factory = session_factory
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)

    with session_factory() as session:
        t = Task(session_id=conv.id, task_text="Task text")
        session.add(t)
        session.flush()
        run = WorkerRun(
            task_id=t.id,
            session_id=conv.id,
            worker_type="codex",
            started_at=datetime.now(UTC),
            status=WorkerRunStatus.SUCCESS,
            delivery_metadata={"ci_status": None},
        )
        session.add(run)
        session.commit()
        run_id = run.id

    scheduler._update_run_ci_metadata(run_id, "failed", [{"name": "lint"}])

    with session_factory() as session:
        db_run = session.get(WorkerRun, run_id)
        assert db_run.delivery_metadata["ci_status"] == "failed"
        assert db_run.delivery_metadata["ci_failed_jobs"] == ["lint"]


def test_submit_repair_task():
    task_service = MagicMock()
    outcome = MagicMock()
    outcome.duplicate = False
    outcome.task_snapshot.task_id = "repaired-t1"
    task_service.create_task_outcome.return_value = outcome

    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)

    with patch.object(scheduler, "_fetch_logs", return_value="Log data failure"):
        scheduler._submit_repair_task(
            task_id="t1",
            repo_url="https://github.com/org/repo",
            repo_spec="org/repo",
            branch_name="feat-1",
            head_sha="sha123",
            check={"name": "test", "link": "http://link"},
            env={},
        )
        task_service.create_task_outcome.assert_called_once()


@pytest.mark.asyncio
async def test_parse_logs_with_llm_async_branches():
    task_service = MagicMock()
    config_none = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        ci_polling_enabled=True,
        ci_polling_llm_profile="none",
    )
    scheduler_none = CIPollingScheduler(task_service, config_none)
    assert await scheduler_none._parse_logs_with_llm_async("logs", "check") == "logs"

    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        ci_polling_enabled=True,
        ci_polling_llm_profile="default",
    )
    scheduler = CIPollingScheduler(task_service, config)

    # Worker facade is None
    task_service.worker = None
    assert await scheduler._parse_logs_with_llm_async("logs", "check") == "logs"

    # Worker error
    worker = MagicMock()
    worker.run = AsyncMock(side_effect=RuntimeError("Worker error"))
    task_service.worker = worker
    assert await scheduler._parse_logs_with_llm_async("logs", "check") == "logs"


def test_fetch_logs():
    task_service = MagicMock()
    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        ci_polling_enabled=True,
        ci_polling_log_limit_bytes=100,
    )
    scheduler = CIPollingScheduler(task_service, config)

    with patch("subprocess.run") as mock_run:
        # Match URL run ID
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="A" * 150)
        logs = scheduler._fetch_logs(
            "org/repo", "sha123", {"link": "https://github.com/org/repo/actions/runs/999"}, {}
        )
        assert len(logs) == 100

        # No URL match, list runs
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    [{"databaseId": 888, "workflowName": "test", "conclusion": "failure"}]
                ),
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="Build failed logs"),
        ]
        logs2 = scheduler._fetch_logs("org/repo", "sha123", {"name": "test"}, {})
        assert logs2 == "Build failed logs"
