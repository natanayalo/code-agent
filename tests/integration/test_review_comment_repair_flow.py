"""Integration test for review comment repair flow: poll -> spawn -> complete -> reply."""

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


def _setup_db() -> Any:
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _seed_parent_task(session_factory: Any) -> tuple[str, str, int]:
    with session_factory() as session:
        user = User(external_user_id="owner_user", display_name="Owner")
        session.add(user)
        session.flush()

        parent_session = Session(user_id=user.id, channel="web", external_thread_id="thread_1")
        session.add(parent_session)
        session.flush()

        parent_task = Task(
            session_id=parent_session.id,
            task_text="Add feature X",
            repo_url="https://github.com/owner/repo",
            branch="feat/x",
            status=TaskStatus.COMPLETED,
        )
        session.add(parent_task)
        session.flush()

        parent_run = WorkerRun(
            task_id=parent_task.id,
            session_id=parent_session.id,
            worker_type="codex",
            started_at=datetime.now(UTC),
            status=WorkerRunStatus.SUCCESS,
            delivery_metadata={
                "delivery_mode": "draft_pr",
                "branch_name": "feat/x",
                "pr_number": 12,
                "head_sha": "sha123",
                "ci_status": "passed",
            },
        )
        session.add(parent_run)
        session.commit()

        return parent_task.id, parent_run.id, user.id


def _make_task_creator(session_factory: Any, user_id: int, parent_task_id: str) -> Any:
    child_id_holder = {"id": None}

    def mock_create_task_outcome(submission: Any, delivery_key: Any) -> Any:
        with session_factory() as session:
            rc_session = Session(
                user_id=user_id,
                channel="review_comment_polling",
                external_thread_id=parent_task_id,
            )
            session.add(rc_session)
            session.flush()

            child_task = Task(
                session_id=rc_session.id,
                task_text=submission.task_text,
                repair_for_task_id=submission.repair_for_task_id,
                constraints=submission.constraints,
                status=TaskStatus.COMPLETED,
            )
            session.add(child_task)
            session.flush()

            child_run = WorkerRun(
                task_id=child_task.id,
                session_id=rc_session.id,
                worker_type="codex",
                started_at=datetime.now(UTC),
                status=WorkerRunStatus.SUCCESS,
                summary="Added docstring to src/utils.py as requested.",
            )
            session.add(child_run)
            session.commit()

            child_id_holder["id"] = child_task.id

        outcome = MagicMock()
        outcome.duplicate = False
        outcome.task_snapshot.task_id = child_id_holder["id"]
        return outcome

    return mock_create_task_outcome


def test_review_comment_repair_and_reply_flow(monkeypatch: Any) -> None:
    session_factory = _setup_db()
    parent_task_id, parent_run_id, user_id = _seed_parent_task(session_factory)

    review_comment = ReviewComment(
        id=999,
        path="src/utils.py",
        line=42,
        body="Please add docstring",
        user_login="owner_login",
        created_at="2026-08-07T14:00:00Z",
        updated_at="2026-08-07T14:00:00Z",
    )

    monkeypatch.setattr("orchestrator.github_reviews.fetch_repo_owner", lambda r, t: "owner_login")
    monkeypatch.setattr(
        "orchestrator.github_reviews.fetch_review_comments",
        lambda r, pr, t, since=None, owner_login=None: [review_comment],
    )

    posted_replies: list[dict[str, Any]] = []

    def mock_reply(repo_spec: str, pr_num: int, comment_id: int, body: str, token: str) -> bool:
        posted_replies.append(
            {"repo_spec": repo_spec, "pr_number": pr_num, "comment_id": comment_id, "body": body}
        )
        return True

    monkeypatch.setattr("orchestrator.github_reviews.reply_to_review_comment", mock_reply)

    task_service = MagicMock()
    task_service.session_factory = session_factory
    task_service.create_task_outcome = _make_task_creator(session_factory, user_id, parent_task_id)

    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        ci_polling_enabled=True,
        review_comment_max_repair_passes=3,
    )
    scheduler = CIPollingScheduler(task_service, config)

    run_info = {
        "run_id": parent_run_id,
        "task_id": parent_task_id,
        "repo_url": "https://github.com/owner/repo",
        "secrets": {"GH_TOKEN": "token123"},
        "delivery_metadata": {
            "delivery_mode": "draft_pr",
            "branch_name": "feat/x",
            "pr_number": 12,
            "head_sha": "sha123",
            "ci_status": "passed",
        },
    }

    scheduler._poll_run(run_info)

    assert len(posted_replies) == 1
    reply = posted_replies[0]
    assert reply["comment_id"] == 999
    assert reply["pr_number"] == 12
    assert "Added docstring to src/utils.py" in reply["body"]

    with session_factory() as session:
        run = session.get(WorkerRun, parent_run_id)
        assert run is not None
        meta = run.delivery_metadata
        assert meta.get("review_comments_replied_at") is not None
        comments = meta.get("review_comments") or []
        assert len(comments) == 1
        assert comments[0]["id"] == 999
        assert comments[0]["replied"] is True
