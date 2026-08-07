"""Unit tests for review comment polling and repair task spawning in CIPollingScheduler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from sqlalchemy.pool import StaticPool

from apps.api.ci_polling import CIPollingScheduler
from apps.api.config import SystemConfig
from db.base import Base
from db.enums import TaskStatus, WorkerRunStatus
from db.models import Session, Task, User, WorkerRun
from orchestrator.github_reviews import ReviewComment
from repositories import create_engine_from_url, create_session_factory


def _setup_test_db() -> Any:
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _seed_task_and_run(session_factory: Any) -> tuple[str, str]:
    with session_factory() as session:
        user = User(external_user_id="u1", display_name="User 1")
        session.add(user)
        session.flush()
        db_session = Session(user_id=user.id, channel="web", external_thread_id="t1")
        session.add(db_session)
        session.flush()

        task = Task(
            session_id=db_session.id,
            task_text="Original task",
            repo_url="https://github.com/owner/repo",
            branch="feature",
            status=TaskStatus.COMPLETED,
        )
        session.add(task)
        session.flush()

        run = WorkerRun(
            task_id=task.id,
            session_id=db_session.id,
            worker_type="codex",
            started_at=datetime.now(UTC),
            status=WorkerRunStatus.SUCCESS,
            delivery_metadata={
                "delivery_mode": "draft_pr",
                "branch_name": "feature",
                "pr_number": 42,
                "head_sha": "abc1234",
                "ci_status": "passed",
            },
        )
        session.add(run)
        session.commit()
        return run.id, task.id


def test_poll_review_comments_spawns_repair_task(monkeypatch: Any) -> None:
    session_factory = _setup_test_db()
    run_id, task_id = _seed_task_and_run(session_factory)

    comment = ReviewComment(
        id=555,
        path="src/main.py",
        line=12,
        body="Fix typo here",
        user_login="owner",
        created_at="2026-08-07T12:00:00Z",
        updated_at="2026-08-07T12:00:00Z",
    )

    monkeypatch.setattr("orchestrator.github_reviews.fetch_repo_owner", lambda r, t: "owner")
    monkeypatch.setattr(
        "orchestrator.github_reviews.fetch_review_comments",
        lambda r, pr, t, since=None, owner_login=None: [comment],
    )

    task_service = MagicMock()
    task_service.session_factory = session_factory
    outcome_mock = MagicMock()
    outcome_mock.duplicate = False
    outcome_mock.task_snapshot.task_id = "child_task_123"
    task_service.create_task_outcome.return_value = outcome_mock

    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        ci_polling_enabled=True,
        review_comment_max_repair_passes=3,
    )
    scheduler = CIPollingScheduler(task_service, config)

    run_info = {
        "run_id": run_id,
        "task_id": task_id,
        "repo_url": "https://github.com/owner/repo",
        "secrets": {"GH_TOKEN": "token123"},
        "delivery_metadata": {
            "delivery_mode": "draft_pr",
            "branch_name": "feature",
            "pr_number": 42,
            "head_sha": "abc1234",
            "ci_status": "passed",
        },
    }

    scheduler._poll_run(run_info)

    task_service.create_task_outcome.assert_called_once()
    submission = task_service.create_task_outcome.call_args[0][0]
    assert submission.repair_for_task_id == task_id
    assert submission.constraints["review_comment_ids"] == [555]

    with session_factory() as session:
        updated_run = session.get(WorkerRun, run_id)
        assert updated_run is not None
        meta = updated_run.delivery_metadata
        assert 555 in meta["processed_review_comment_ids"]
        assert meta["review_comments"][0]["id"] == 555


def _seed_parent_and_repairs(session_factory: Any) -> tuple[str, str]:
    with session_factory() as session:
        user = User(external_user_id="u1", display_name="User 1")
        session.add(user)
        session.flush()
        db_session = Session(user_id=user.id, channel="web", external_thread_id="t1")
        session.add(db_session)
        session.flush()

        parent_task = Task(
            session_id=db_session.id,
            task_text="Parent task",
            repo_url="https://github.com/owner/repo",
            branch="feature",
            status=TaskStatus.COMPLETED,
        )
        session.add(parent_task)
        session.flush()

        for i in range(3):
            rc_session = Session(
                user_id=user.id, channel="review_comment_polling", external_thread_id=f"rc_{i}"
            )
            session.add(rc_session)
            session.flush()
            child_task = Task(
                session_id=rc_session.id,
                task_text=f"Repair {i}",
                repair_for_task_id=parent_task.id,
                status=TaskStatus.COMPLETED,
            )
            session.add(child_task)

        run = WorkerRun(
            task_id=parent_task.id,
            session_id=db_session.id,
            worker_type="codex",
            started_at=datetime.now(UTC),
            status=WorkerRunStatus.SUCCESS,
            delivery_metadata={
                "delivery_mode": "draft_pr",
                "branch_name": "feature",
                "pr_number": 42,
                "head_sha": "abc1234",
            },
        )
        session.add(run)
        session.commit()
        return run.id, parent_task.id


def test_poll_review_comments_budget_exhausted(monkeypatch: Any) -> None:
    session_factory = _setup_test_db()
    run_id, parent_task_id = _seed_parent_and_repairs(session_factory)

    comment = ReviewComment(
        id=777,
        path="src/main.py",
        line=5,
        body="Another comment",
        user_login="owner",
        created_at="2026-08-07T12:00:00Z",
        updated_at="2026-08-07T12:00:00Z",
    )

    monkeypatch.setattr("orchestrator.github_reviews.fetch_repo_owner", lambda r, t: "owner")
    monkeypatch.setattr(
        "orchestrator.github_reviews.fetch_review_comments",
        lambda r, pr, t, since=None, owner_login=None: [comment],
    )

    task_service = MagicMock()
    task_service.session_factory = session_factory
    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        ci_polling_enabled=True,
        review_comment_max_repair_passes=3,
    )
    scheduler = CIPollingScheduler(task_service, config)

    run_info = {
        "run_id": run_id,
        "task_id": parent_task_id,
        "repo_url": "https://github.com/owner/repo",
        "secrets": {"GH_TOKEN": "token123"},
        "delivery_metadata": {
            "delivery_mode": "draft_pr",
            "branch_name": "feature",
            "pr_number": 42,
            "head_sha": "abc1234",
        },
    }

    scheduler._poll_run(run_info)

    task_service.create_task_outcome.assert_not_called()
