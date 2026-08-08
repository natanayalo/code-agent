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

        run_no_meta = WorkerRun(
            task_id=t.id,
            session_id=conv.id,
            worker_type="codex",
            started_at=datetime.now(UTC) - timedelta(hours=1),
            status=WorkerRunStatus.SUCCESS,
            delivery_metadata=None,
        )
        session.add(run_no_meta)

        run_summary = WorkerRun(
            task_id=t.id,
            session_id=conv.id,
            worker_type="codex",
            started_at=datetime.now(UTC) - timedelta(hours=1),
            status=WorkerRunStatus.SUCCESS,
            delivery_metadata={"delivery_mode": "summary"},
        )
        session.add(run_summary)
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

    # Missing run
    scheduler._update_run_ci_metadata("non-existent-run-id", "passed", [])

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
            delivery_metadata=None,
        )
        session.add(run)
        session.commit()
        run_no_meta_id = run.id

    # Run with no delivery_metadata
    scheduler._update_run_ci_metadata(run_no_meta_id, "passed", [])

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


def test_ci_polling_start_already_running(monkeypatch):
    task_service = MagicMock()
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)
    scheduler._running = True
    scheduler.start()
    assert scheduler._running is True


def test_ci_polling_start_no_gh_cli(monkeypatch):
    task_service = MagicMock()
    task_service.session_factory = MagicMock()
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    async def run_test():
        scheduler.start()
        assert scheduler._running is True
        await scheduler.stop()

    asyncio.run(run_test())


def test_get_pending_runs_none_session_factory():
    task_service = MagicMock()
    task_service.session_factory = None
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)
    assert scheduler._get_pending_runs() == []
    scheduler.tick()  # early return when ci_polling_enabled is false or no pending runs


def test_poll_run_missing_gh_token(monkeypatch):
    task_service = MagicMock()
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    run_info = {
        "run_id": "r1",
        "task_id": "t1",
        "repo_url": "https://github.com/org/repo",
        "secrets": {},
        "delivery_metadata": {
            "branch_name": "main",
            "head_sha": "sha123",
        },
    }
    scheduler._poll_run(run_info)


def test_poll_run_invalid_fields_or_repo_spec(monkeypatch):
    task_service = MagicMock()
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)
    monkeypatch.setenv("GH_TOKEN", "token")

    # Missing branch_name
    scheduler._poll_run(
        {
            "run_id": "r1",
            "task_id": "t1",
            "repo_url": "https://github.com/org/repo",
            "delivery_metadata": {},
        }
    )

    # Invalid repo url
    scheduler._poll_run(
        {
            "run_id": "r1",
            "task_id": "t1",
            "repo_url": "not-a-url",
            "delivery_metadata": {"branch_name": "b", "head_sha": "s"},
        }
    )


def test_poll_run_checks_failed_submits_repair(monkeypatch):
    task_service = MagicMock()
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)
    monkeypatch.setenv("GH_TOKEN", "token")

    failed_check = {
        "name": "test-check",
        "state": "FAILURE",
        "link": "https://github.com/org/repo/runs/1",
    }
    monkeypatch.setattr(
        scheduler,
        "_get_pr_checks",
        lambda b, r, rid, env: ["non-dict", failed_check],
    )
    monkeypatch.setattr(scheduler, "_update_run_ci_metadata", lambda rid, st, fc: None)
    monkeypatch.setattr(
        scheduler,
        "_submit_repair_task",
        lambda tid, url, spec, b, sha, c, env: None,
    )
    monkeypatch.setattr("orchestrator.github_reviews.poll_review_comments", lambda *args: None)
    monkeypatch.setattr(
        "orchestrator.github_reviews.process_review_comment_replies",
        lambda *args: None,
    )

    scheduler._poll_run(
        {
            "run_id": "r1",
            "task_id": "t1",
            "repo_url": "https://github.com/org/repo",
            "delivery_metadata": {"branch_name": "b1", "head_sha": "sha1"},
        }
    )


def test_fetch_logs_limit_zero_or_negative():
    task_service = MagicMock()
    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        ci_polling_enabled=True,
        ci_polling_log_limit_bytes=0,
    )
    scheduler = CIPollingScheduler(task_service, config)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="logs")
        res = scheduler._fetch_logs("org/repo", "sha", {"link": "/runs/123"}, {})
        assert res == ""


def test_submit_repair_task_no_running_event_loop(monkeypatch):
    task_service = MagicMock()
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)
    scheduler._loop_ref = None

    monkeypatch.setattr(
        scheduler,
        "_fetch_logs",
        lambda repo_spec, sha, check, env: "Log error output",
    )
    task_service.create_task_outcome.return_value = MagicMock(
        duplicate=False,
        task_snapshot=MagicMock(task_id="r100"),
    )

    scheduler._submit_repair_task(
        "t1",
        "https://github.com/org/repo",
        "org/repo",
        "main",
        "sha",
        {"name": "test-check"},
        {},
    )
    task_service.create_task_outcome.assert_called_once()


def test_poll_run_failed_check_parsing(monkeypatch):
    task_service = MagicMock()
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)
    monkeypatch.setenv("GH_TOKEN", "token")

    failed_check = {
        "name": "test-check",
        "state": "FAILURE",
        "link": "https://github.com/org/repo/runs/1",
    }
    monkeypatch.setattr(
        scheduler,
        "_get_pr_checks",
        lambda b, r, rid, env: [failed_check, {"state": "SUCCESS"}],
    )
    monkeypatch.setattr(scheduler, "_update_run_ci_metadata", lambda rid, st, fc: None)
    monkeypatch.setattr(
        scheduler,
        "_submit_repair_task",
        lambda tid, url, spec, b, sha, c, env: None,
    )
    monkeypatch.setattr(
        "orchestrator.github_reviews.poll_review_comments",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "orchestrator.github_reviews.process_review_comment_replies",
        lambda *args: None,
    )

    scheduler._poll_run(
        {
            "run_id": "r1",
            "task_id": "t1",
            "repo_url": "https://github.com/org/repo",
            "delivery_metadata": {"branch_name": "b1", "head_sha": "sha1"},
        }
    )


def test_submit_repair_task_with_llm_parsing_async(monkeypatch):
    task_service = MagicMock()
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)

    async def run_test():
        scheduler._loop_ref = asyncio.get_running_loop()
        monkeypatch.setattr(
            scheduler,
            "_fetch_logs",
            lambda repo_spec, sha, check, env: "Error logs output",
        )
        monkeypatch.setattr(
            scheduler,
            "_parse_logs_with_llm_async",
            AsyncMock(return_value="Parsed summary"),
        )

        task_service.create_task_outcome.return_value = MagicMock(
            duplicate=False,
            task_snapshot=MagicMock(task_id="child_1"),
        )


def test_get_pr_checks_no_pull_requests(monkeypatch):
    task_service = MagicMock()
    config = SystemConfig(default_image="img", workspace_root="/tmp", ci_polling_enabled=True)
    scheduler = CIPollingScheduler(task_service, config)
    monkeypatch.setattr(scheduler, "_update_run_ci_metadata", MagicMock())

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stderr="no open pull requests found for branch main"
        )
        res = scheduler._get_pr_checks("main", "org/repo", "run1", {})
        assert res is None
        scheduler._update_run_ci_metadata.assert_called_once_with("run1", "not_found", [])


def test_fetch_logs_fallback_workflow():
    task_service = MagicMock()
    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        ci_polling_enabled=True,
        ci_polling_log_limit_bytes=50,
    )
    scheduler = CIPollingScheduler(task_service, config)

    with patch("subprocess.run") as mock_run:
        # First list runs returns workflows that don't match name, but fallback finds failed run
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    [
                        {"databaseId": 111, "workflowName": "other", "conclusion": "success"},
                        {"databaseId": 222, "workflowName": "diff", "conclusion": "failure"},
                    ]
                ),
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="A" * 100),
        ]
        logs = scheduler._fetch_logs("org/repo", "sha123", {"name": "test"}, {})
        assert len(logs) == 50
