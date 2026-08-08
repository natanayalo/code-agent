"""Unit tests for orchestrator/github_reviews.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock
from urllib.error import HTTPError
from urllib.request import Request

from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import TaskStatus, WorkerRunStatus
from db.models import Session, Task, User, WorkerRun
from orchestrator.github_reviews import (
    ReviewComment,
    build_review_comment_repair_prompt,
    count_review_comment_repairs,
    fetch_repo_owner,
    fetch_review_comments,
    poll_review_comments,
    process_review_comment_replies,
    reply_to_review_comment,
    update_run_review_comment_metadata,
)
from repositories import create_engine_from_url, create_session_factory


class MockHTTPResponse:
    def __init__(self, data: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._raw = json.dumps(data).encode("utf-8")
        self.status = status
        self.headers = headers or {}

    def __enter__(self) -> MockHTTPResponse:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def read(self) -> bytes:
        return self._raw


def _setup_test_db() -> Any:
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def test_fetch_repo_owner_env_override(monkeypatch: Any) -> None:
    monkeypatch.setenv("CODE_AGENT_GITHUB_OWNER_LOGIN", "override_owner")
    owner = fetch_repo_owner("owner/repo", "token123")
    assert owner == "override_owner"


def test_fetch_repo_owner_invalid_spec(monkeypatch: Any) -> None:
    monkeypatch.delenv("CODE_AGENT_GITHUB_OWNER_LOGIN", raising=False)
    assert fetch_repo_owner("invalid_repo", "token123") is None


def test_fetch_repo_owner_success(monkeypatch: Any) -> None:
    monkeypatch.delenv("CODE_AGENT_GITHUB_OWNER_LOGIN", raising=False)

    def mock_urlopen(req: Request, timeout: int = 30) -> MockHTTPResponse:
        assert req.full_url == "https://api.github.com/repos/myorg/myrepo"
        assert req.headers["Authorization"] == "Bearer token123"
        return MockHTTPResponse({"owner": {"login": "myorg"}})

    monkeypatch.setattr("orchestrator.github_reviews.urlopen", mock_urlopen)
    owner = fetch_repo_owner("myorg/myrepo", "token123")
    assert owner == "myorg"


def test_fetch_repo_owner_exception(monkeypatch: Any) -> None:
    monkeypatch.delenv("CODE_AGENT_GITHUB_OWNER_LOGIN", raising=False)

    def mock_urlopen(req: Request, timeout: int = 30) -> MockHTTPResponse:
        raise HTTPError(req.full_url, 500, "Server Error", {}, BytesIO())

    monkeypatch.setattr("orchestrator.github_reviews.urlopen", mock_urlopen)
    owner = fetch_repo_owner("myorg/myrepo", "token123")
    assert owner == "myorg"


def test_fetch_review_comments_invalid_args() -> None:
    assert fetch_review_comments("invalid_repo", 1, "token") == []
    assert fetch_review_comments("owner/repo", 0, "token") == []


def test_fetch_review_comments_filters_by_owner(monkeypatch: Any) -> None:
    raw_response = [
        "not-a-dict",
        {
            "id": 101,
            "path": "app.py",
            "line": 15,
            "body": "Fix variable name",
            "user": {"login": "alice"},
            "created_at": "2026-08-07T12:00:00Z",
            "updated_at": "2026-08-07T12:00:00Z",
            "diff_hunk": "@@ -10,5 +10,5 @@",
            "html_url": "https://github.com/owner/repo/pull/1#discussion_r101",
        },
        {
            "id": 102,
            "path": "app.py",
            "line": 20,
            "body": "Comment from bot",
            "user": {"login": "github-actions[bot]"},
            "created_at": "2026-08-07T12:05:00Z",
            "updated_at": "2026-08-07T12:05:00Z",
        },
        {"id": 103, "path": "app.py", "user": {"login": "alice"}},  # missing body
        {"path": "app.py", "body": "missing id", "user": {"login": "alice"}},
        {"id": 104, "body": "missing path", "user": {"login": "alice"}},
        {"id": 0, "path": "app.py", "body": "zero id", "user": {"login": "alice"}},
        {"id": 105, "path": "", "body": "empty path", "user": {"login": "alice"}},
        {"id": 106, "path": "app.py", "body": "", "user": {"login": "alice"}},
    ]

    def mock_urlopen(req: Request, timeout: int = 30) -> MockHTTPResponse:
        assert "pulls/1/comments" in req.full_url
        return MockHTTPResponse(raw_response)

    monkeypatch.setattr("orchestrator.github_reviews.urlopen", mock_urlopen)
    comments = fetch_review_comments("owner/repo", 1, "token123", owner_login="alice")
    assert len(comments) == 1
    assert comments[0].id == 101
    assert comments[0].user_login == "alice"


def test_fetch_review_comments_exception_and_invalid_type(monkeypatch: Any) -> None:
    def mock_urlopen_err(req: Request, timeout: int = 30) -> MockHTTPResponse:
        raise OSError("Network error")

    monkeypatch.setattr("orchestrator.github_reviews.urlopen", mock_urlopen_err)
    assert fetch_review_comments("owner/repo", 1, "token123") == []

    def mock_urlopen_dict(req: Request, timeout: int = 30) -> MockHTTPResponse:
        return MockHTTPResponse({"message": "Not Found"})

    monkeypatch.setattr("orchestrator.github_reviews.urlopen", mock_urlopen_dict)
    assert fetch_review_comments("owner/repo", 1, "token123") == []


def test_fetch_review_comments_since_filter(monkeypatch: Any) -> None:
    def mock_urlopen(req: Request, timeout: int = 30) -> MockHTTPResponse:
        assert "since=2026-08-07T10%3A00%3A00Z" in req.full_url
        return MockHTTPResponse([])

    monkeypatch.setattr("orchestrator.github_reviews.urlopen", mock_urlopen)
    comments = fetch_review_comments("owner/repo", 1, "token123", since="2026-08-07T10:00:00Z")
    assert comments == []


def test_fetch_review_comments_pagination(monkeypatch: Any) -> None:
    c1 = {
        "id": 1,
        "path": "app.py",
        "body": "First",
        "user": {"login": "alice"},
        "created_at": "2026-08-07T12:00:00Z",
        "updated_at": "2026-08-07T12:00:00Z",
    }
    c2 = {
        "id": 2,
        "path": "app.py",
        "body": "Second",
        "user": {"login": "alice"},
        "created_at": "2026-08-07T12:01:00Z",
        "updated_at": "2026-08-07T12:01:00Z",
    }

    requests_made = []

    def mock_urlopen(req: Request, timeout: int = 30) -> MockHTTPResponse:
        requests_made.append(req.full_url)
        if "&page=1" in req.full_url:
            headers = {"Link": '<https://api.github.com/...>; rel="next"'}
            return MockHTTPResponse([c1] * 100, headers=headers)
        return MockHTTPResponse([c2], headers={})

    monkeypatch.setattr("orchestrator.github_reviews.urlopen", mock_urlopen)
    comments = fetch_review_comments("owner/repo", 1, "token123", owner_login="alice")
    assert len(comments) == 101
    assert len(requests_made) == 2


def test_reply_to_review_comment_invalid_args() -> None:
    assert reply_to_review_comment("invalid_repo", 1, 101, "body", "token") is False
    assert reply_to_review_comment("owner/repo", 0, 101, "body", "token") is False
    assert reply_to_review_comment("owner/repo", 1, 0, "body", "token") is False


def test_reply_to_review_comment_success(monkeypatch: Any) -> None:
    def mock_urlopen(req: Request, timeout: int = 30) -> MockHTTPResponse:
        assert req.method == "POST"
        assert (
            req.full_url == "https://api.github.com/repos/owner/repo/pulls/1/comments/101/replies"
        )
        data = json.loads(req.data.decode("utf-8"))
        assert data["body"] == "Fixed!"
        return MockHTTPResponse({"id": 201}, status=201)

    monkeypatch.setattr("orchestrator.github_reviews.urlopen", mock_urlopen)
    success = reply_to_review_comment("owner/repo", 1, 101, "Fixed!", "token123")
    assert success is True


def test_reply_to_review_comment_failure(monkeypatch: Any) -> None:
    def mock_urlopen(req: Request, timeout: int = 30) -> MockHTTPResponse:
        raise HTTPError(req.full_url, 404, "Not Found", {}, BytesIO())

    monkeypatch.setattr("orchestrator.github_reviews.urlopen", mock_urlopen)
    success = reply_to_review_comment("owner/repo", 1, 101, "Fixed!", "token123")
    assert success is False

    def mock_urlopen_oserr(req: Request, timeout: int = 30) -> MockHTTPResponse:
        raise OSError("Timeout")

    monkeypatch.setattr("orchestrator.github_reviews.urlopen", mock_urlopen_oserr)
    assert reply_to_review_comment("owner/repo", 1, 101, "Fixed!", "token123") is False


def test_build_review_comment_repair_prompt() -> None:
    assert "Please inspect and resolve" in build_review_comment_repair_prompt([])

    c1 = ReviewComment(
        id=1,
        path="main.py",
        line=10,
        body="Use explicit variable name",
        diff_hunk="@@ -10,1 +10,1 @@",
        user_login="alice",
        created_at="2026-08-07T12:00:00Z",
        updated_at="2026-08-07T12:00:00Z",
    )
    prompt = build_review_comment_repair_prompt([c1])
    assert "File: `main.py`" in prompt
    assert "line 10 (by @alice)" in prompt
    assert "Use explicit variable name" in prompt
    assert "@@ -10,1 +10,1 @@" in prompt


def test_count_and_update_helpers_none_session_factory() -> None:
    assert count_review_comment_repairs(None, "task1") == 0
    update_run_review_comment_metadata(None, "run1", [])
    process_review_comment_replies(None, {"task_id": "task1"}, "owner/repo", "token")


def test_poll_review_comments_edge_cases(monkeypatch: Any) -> None:
    task_service = MagicMock()
    config = MagicMock()
    config.review_comment_max_repair_passes = 3

    # Missing pr_number
    poll_review_comments(None, task_service, config, {"task_id": "t1"}, "owner/repo", "token")

    # Empty comments
    monkeypatch.setattr(
        "orchestrator.github_reviews.fetch_review_comments",
        lambda r, pr, t, since=None, owner_login=None: [],
    )
    poll_review_comments(
        None,
        task_service,
        config,
        {"task_id": "t1", "delivery_metadata": {"pr_number": 1}},
        "owner/repo",
        "token",
    )

    # All comments already processed
    comment = ReviewComment(
        id=888,
        path="file.py",
        body="Fix",
        user_login="owner",
        created_at="2026-08-07T12:00:00Z",
        updated_at="2026-08-07T12:00:00Z",
    )
    monkeypatch.setattr(
        "orchestrator.github_reviews.fetch_review_comments",
        lambda r, pr, t, since=None, owner_login=None: [comment],
    )
    poll_review_comments(
        None,
        task_service,
        config,
        {
            "task_id": "t1",
            "delivery_metadata": {"pr_number": 1, "processed_review_comment_ids": [888]},
        },
        "owner/repo",
        "token",
    )

    # Exception during task creation
    task_service.create_task_outcome.side_effect = RuntimeError("Task creation failed")
    poll_review_comments(
        None,
        task_service,
        config,
        {"task_id": "t1", "run_id": "r1", "delivery_metadata": {"pr_number": 1}},
        "owner/repo",
        "token",
    )


def test_update_run_review_comment_metadata_missing_run(monkeypatch: Any) -> None:
    session_factory = _setup_test_db()
    c = ReviewComment(
        id=1,
        path="a.py",
        body="b",
        user_login="u",
        created_at="2026-08-07T12:00:00Z",
        updated_at="2026-08-07T12:00:00Z",
    )
    update_run_review_comment_metadata(session_factory, "non-existent-run-id", [c])


def test_process_review_comment_replies_in_progress_task(monkeypatch: Any) -> None:
    session_factory = _setup_test_db()

    with session_factory() as session:
        user = User(external_user_id="u1", display_name="User 1")
        session.add(user)
        session.flush()

        p_session = Session(user_id=user.id, channel="web", external_thread_id="t1")
        session.add(p_session)
        session.flush()

        parent_task = Task(
            session_id=p_session.id,
            task_text="Parent task",
            status=TaskStatus.IN_PROGRESS,
        )
        session.add(parent_task)
        session.flush()

        parent_run = WorkerRun(
            task_id=parent_task.id,
            session_id=p_session.id,
            worker_type="codex",
            started_at=datetime.now(UTC),
            status=WorkerRunStatus.SUCCESS,
            delivery_metadata={"pr_number": 10},
        )
        session.add(parent_run)
        session.flush()

        rc_session = Session(
            user_id=user.id, channel="review_comment_polling", external_thread_id=parent_task.id
        )
        session.add(rc_session)
        session.flush()

        active_child = Task(
            session_id=rc_session.id,
            task_text="Active repair",
            repair_for_task_id=parent_task.id,
            constraints={"review_comment_ids": [404]},
            status=TaskStatus.IN_PROGRESS,
        )
        session.add(active_child)
        session.commit()

        p_task_id = parent_task.id
        p_run_id = parent_run.id

    run_info = {
        "task_id": p_task_id,
        "run_id": p_run_id,
        "delivery_metadata": {"pr_number": 10},
    }
    process_review_comment_replies(session_factory, run_info, "owner/repo", "token")


def test_process_review_comment_replies_failed_task(monkeypatch: Any) -> None:
    session_factory = _setup_test_db()

    with session_factory() as session:
        user = User(external_user_id="u1", display_name="User 1")
        session.add(user)
        session.flush()

        p_session = Session(user_id=user.id, channel="web", external_thread_id="t1")
        session.add(p_session)
        session.flush()

        parent_task = Task(
            session_id=p_session.id,
            task_text="Parent task",
            status=TaskStatus.COMPLETED,
        )
        session.add(parent_task)
        session.flush()

        parent_run = WorkerRun(
            task_id=parent_task.id,
            session_id=p_session.id,
            worker_type="codex",
            started_at=datetime.now(UTC),
            status=WorkerRunStatus.SUCCESS,
            delivery_metadata={"pr_number": 10},
        )
        session.add(parent_run)
        session.flush()

        rc_session = Session(
            user_id=user.id, channel="review_comment_polling", external_thread_id=parent_task.id
        )
        session.add(rc_session)
        session.flush()

        failed_child = Task(
            session_id=rc_session.id,
            task_text="Failed repair",
            repair_for_task_id=parent_task.id,
            constraints={"review_comment_ids": [404]},
            status=TaskStatus.FAILED,
            last_error="Syntax error in fix",
        )
        session.add(failed_child)
        session.commit()

        p_task_id = parent_task.id
        p_run_id = parent_run.id

    replies = []
    monkeypatch.setattr(
        "orchestrator.github_reviews.reply_to_review_comment",
        lambda repo_spec, pr_num, cid, body, token: replies.append((cid, body)) or True,
    )

    run_info = {
        "task_id": p_task_id,
        "run_id": p_run_id,
        "delivery_metadata": {"pr_number": 10},
    }
    process_review_comment_replies(session_factory, run_info, "owner/repo", "token")

    assert len(replies) == 1
    assert replies[0][0] == 404
    assert "failed" in replies[0][1]
    assert "Syntax error in fix" in replies[0][1]


def test_process_review_comment_replies_deduplication(monkeypatch: Any) -> None:
    session_factory = _setup_test_db()

    with session_factory() as session:
        user = User(external_user_id="u1", display_name="User 1")
        session.add(user)
        session.flush()

        p_session = Session(user_id=user.id, channel="web", external_thread_id="t1")
        session.add(p_session)
        session.flush()

        parent_task = Task(session_id=p_session.id, task_text="Parent", status=TaskStatus.COMPLETED)
        session.add(parent_task)
        session.flush()

        parent_run = WorkerRun(
            task_id=parent_task.id,
            session_id=p_session.id,
            worker_type="codex",
            started_at=datetime.now(UTC),
            status=WorkerRunStatus.SUCCESS,
            delivery_metadata={"pr_number": 10},
        )
        session.add(parent_run)
        session.flush()

        rc_session = Session(
            user_id=user.id, channel="review_comment_polling", external_thread_id=parent_task.id
        )
        session.add(rc_session)
        session.flush()

        # Pass 1: Failed
        child1 = Task(
            session_id=rc_session.id,
            task_text="Repair pass 1",
            repair_for_task_id=parent_task.id,
            constraints={"review_comment_ids": [505]},
            status=TaskStatus.FAILED,
            last_error="Pass 1 failed",
        )
        # Pass 2: Completed
        child2 = Task(
            session_id=rc_session.id,
            task_text="Repair pass 2",
            repair_for_task_id=parent_task.id,
            constraints={"review_comment_ids": [505]},
            status=TaskStatus.COMPLETED,
        )
        session.add_all([child1, child2])
        session.commit()

        p_task_id = parent_task.id
        p_run_id = parent_run.id

    replies = []
    monkeypatch.setattr(
        "orchestrator.github_reviews.reply_to_review_comment",
        lambda repo_spec, pr_num, cid, body, token: replies.append((cid, body)) or True,
    )

    run_info = {"task_id": p_task_id, "run_id": p_run_id, "delivery_metadata": {"pr_number": 10}}
    process_review_comment_replies(session_factory, run_info, "owner/repo", "token")

    # Should be deduplicated into 1 reply preserving the latest pass (COMPLETED)
    assert len(replies) == 1
    assert replies[0][0] == 505
    assert "addressed these review comments" in replies[0][1]


def test_process_review_comment_replies_immediate_checkpointing(monkeypatch: Any) -> None:
    session_factory = _setup_test_db()
    comments_meta = [
        {"id": 601, "path": "a.py", "body": "c1", "replied": False},
        {"id": 602, "path": "b.py", "body": "c2", "replied": False},
    ]

    with session_factory() as session:
        user = User(external_user_id="u1", display_name="User 1")
        session.add(user)
        session.flush()

        p_session = Session(user_id=user.id, channel="web", external_thread_id="t1")
        session.add(p_session)
        session.flush()

        parent_task = Task(session_id=p_session.id, task_text="Parent", status=TaskStatus.COMPLETED)
        session.add(parent_task)
        session.flush()

        parent_run = WorkerRun(
            task_id=parent_task.id,
            session_id=p_session.id,
            worker_type="codex",
            started_at=datetime.now(UTC),
            status=WorkerRunStatus.SUCCESS,
            delivery_metadata={"pr_number": 10, "review_comments": comments_meta},
        )
        rc_session = Session(
            user_id=user.id, channel="review_comment_polling", external_thread_id=parent_task.id
        )
        session.add_all([parent_run, rc_session])
        session.flush()

        child = Task(
            session_id=rc_session.id,
            task_text="Repair pass",
            repair_for_task_id=parent_task.id,
            constraints={"review_comment_ids": [601, 602]},
            status=TaskStatus.COMPLETED,
        )
        session.add(child)
        session.commit()
        p_task_id, p_run_id = parent_task.id, parent_run.id

    def mock_reply(repo_spec: str, pr_num: int, cid: int, body: str, token: str) -> bool:
        if cid == 601:
            return True
        raise RuntimeError("API crash on second comment")

    monkeypatch.setattr("orchestrator.github_reviews.reply_to_review_comment", mock_reply)
    run_info = {
        "task_id": p_task_id,
        "run_id": p_run_id,
        "delivery_metadata": {"pr_number": 10, "review_comments": comments_meta},
    }

    try:
        process_review_comment_replies(session_factory, run_info, "owner/repo", "token")
    except RuntimeError:
        pass

    with session_factory() as session:
        db_run = session.get(WorkerRun, p_run_id)
        assert db_run is not None
        meta = db_run.delivery_metadata
        assert 601 in meta.get("replied_review_comment_ids", [])
        assert 602 not in meta.get("replied_review_comment_ids", [])
        assert any(c["id"] == 601 and c.get("replied") is True for c in meta["review_comments"])
