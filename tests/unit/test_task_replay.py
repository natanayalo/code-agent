"""Unit tests for the task replay mechanism (T-091)."""

from __future__ import annotations

from sqlalchemy.pool import StaticPool

from db.base import Base, utc_now
from db.enums import TaskStatus, WorkerType
from orchestrator import execution as execution_module
from repositories import (
    TaskRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerRequest, WorkerResult


class _StaticWorker(Worker):
    """Minimal worker double used to initialize the service."""

    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(status="success", summary=f"stubbed: {request.task_text}")


def _make_service():
    """Create an in-memory service with a fresh DB schema."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    return service, session_factory


def _create_terminal_task(
    service: execution_module.TaskExecutionService,
    session_factory,
    *,
    status: TaskStatus = TaskStatus.COMPLETED,
    task_text: str = "Fix the bug",
    repo_url: str | None = "https://github.com/example/repo",
    branch: str | None = "main",
    worker_override: WorkerType | None = None,
    constraints: dict | None = None,
    budget: dict | None = None,
) -> str:
    """Create and mark a task as terminal, returning the task_id."""
    submission = execution_module.TaskSubmission(
        task_text=task_text,
        repo_url=repo_url,
        branch=branch,
        worker_override=worker_override,
        constraints=constraints or {},
        budget=budget or {},
    )
    snapshot, _ = service.create_task(submission)
    with session_scope(session_factory) as session:
        TaskRepository(session).update_status(task_id=snapshot.task_id, status=status)
    return snapshot.task_id


def test_replay_completed_task_creates_new_task() -> None:
    """Replaying a completed task should create a fresh task with the same parameters."""
    service, session_factory = _make_service()
    source_id = _create_terminal_task(service, session_factory)

    result = service.replay_task(source_task_id=source_id)

    assert result.status == "created"
    assert result.source_task_id == source_id
    assert result.task_snapshot is not None
    assert result.task_snapshot.task_id != source_id
    assert result.task_snapshot.task_text == "Fix the bug"
    assert result.task_snapshot.repo_url == "https://github.com/example/repo"
    assert result.task_snapshot.branch == "main"
    assert result.task_snapshot.status == TaskStatus.PENDING.value


def test_replay_failed_task_creates_new_task() -> None:
    """Failed tasks should also be replayable."""
    service, session_factory = _make_service()
    source_id = _create_terminal_task(
        service,
        session_factory,
        status=TaskStatus.FAILED,
        task_text="Broken build",
    )

    result = service.replay_task(source_task_id=source_id)

    assert result.status == "created"
    assert result.task_snapshot is not None
    assert result.task_snapshot.task_text == "Broken build"


def test_replay_cancelled_task_creates_new_task() -> None:
    """Cancelled tasks should be replayable."""
    service, session_factory = _make_service()
    source_id = _create_terminal_task(
        service,
        session_factory,
        status=TaskStatus.CANCELLED,
    )

    result = service.replay_task(source_task_id=source_id)

    assert result.status == "created"
    assert result.task_snapshot is not None


def test_replay_with_worker_override() -> None:
    """Replay should accept a worker override that replaces the original."""
    service, session_factory = _make_service()
    source_id = _create_terminal_task(
        service,
        session_factory,
        worker_override=WorkerType.CODEX,
    )

    replay_request = execution_module.TaskReplayRequest(
        worker_override=WorkerType.GEMINI,
    )
    result = service.replay_task(
        source_task_id=source_id,
        replay_request=replay_request,
    )

    assert result.status == "created"
    assert result.task_snapshot is not None
    # The new task snapshot does not expose worker_override directly,
    # but we can verify the constraint provenance and task creation succeeded
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(result.task_snapshot.task_id)
        assert task is not None
        assert task.worker_override == WorkerType.GEMINI


def test_replay_with_constraint_overrides_merges() -> None:
    """Replay constraint overrides should merge with the original constraints."""
    service, session_factory = _make_service()
    source_id = _create_terminal_task(
        service,
        session_factory,
        constraints={
            "requires_approval": True,
            "max_files": 10,
            "nested": {"key": "original"},
        },
    )

    replay_request = execution_module.TaskReplayRequest(
        constraints={"max_files": 20, "new_flag": True, "nested": {"key": "overridden"}},
    )
    result = service.replay_task(
        source_task_id=source_id,
        replay_request=replay_request,
    )

    assert result.status == "created"
    assert result.task_snapshot is not None
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(result.task_snapshot.task_id)
        assert task is not None
        constraints = dict(task.constraints or {})
        # Original key preserved
        assert constraints.get("requires_approval") is True
        # Overridden key updated
        assert constraints.get("max_files") == 20
        # New key added
        assert constraints.get("new_flag") is True
        # Nested key deep merged
        assert constraints.get("nested") == {"key": "overridden"}
        # Provenance tag set as list
        assert constraints.get("replayed_from") == [source_id]


def test_replay_with_budget_overrides_merges() -> None:
    """Replay budget overrides should merge with the original budget."""
    service, session_factory = _make_service()
    source_id = _create_terminal_task(
        service,
        session_factory,
        budget={"max_minutes": 5, "max_iterations": 10},
    )

    replay_request = execution_module.TaskReplayRequest(
        budget={"max_minutes": 15},
    )
    result = service.replay_task(
        source_task_id=source_id,
        replay_request=replay_request,
    )

    assert result.status == "created"
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(result.task_snapshot.task_id)
        assert task is not None
        budget = dict(task.budget or {})
        assert budget.get("max_minutes") == 15
        assert budget.get("max_iterations") == 10


def test_replay_tags_provenance() -> None:
    """Every replayed task should carry a 'replayed_from' provenance tag."""
    service, session_factory = _make_service()
    source_id = _create_terminal_task(service, session_factory)

    result = service.replay_task(source_task_id=source_id)

    assert result.task_snapshot is not None
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(result.task_snapshot.task_id)
        assert task is not None
        assert task.constraints.get("replayed_from") == [source_id]


def test_replay_nonexistent_task_returns_not_found() -> None:
    """Replaying a task that does not exist should return not_found."""
    service, _session_factory = _make_service()

    result = service.replay_task(source_task_id="does-not-exist")

    assert result.status == "not_found"
    assert result.task_snapshot is None
    assert result.source_task_id == "does-not-exist"


def test_replay_pending_task_returns_not_replayable() -> None:
    """Pending (non-terminal) tasks cannot be replayed."""
    service, session_factory = _make_service()
    submission = execution_module.TaskSubmission(task_text="Still pending")
    snapshot, _ = service.create_task(submission)

    result = service.replay_task(source_task_id=snapshot.task_id)

    assert result.status == "not_replayable"
    assert result.task_snapshot is None
    assert "pending" in (result.detail or "").lower()


def test_replay_in_progress_task_returns_not_replayable() -> None:
    """In-progress tasks cannot be replayed."""
    service, session_factory = _make_service()
    submission = execution_module.TaskSubmission(task_text="Running")
    snapshot, _ = service.create_task(submission)
    with session_scope(session_factory) as session:
        TaskRepository(session).update_status(
            task_id=snapshot.task_id, status=TaskStatus.IN_PROGRESS
        )

    result = service.replay_task(source_task_id=snapshot.task_id)

    assert result.status == "not_replayable"
    assert result.task_snapshot is None


def test_replay_of_replay_succeeds() -> None:
    """Replaying a task that was itself a replay should work (chain is allowed)."""
    service, session_factory = _make_service()
    original_id = _create_terminal_task(service, session_factory, task_text="Original task")

    first_replay = service.replay_task(source_task_id=original_id)
    assert first_replay.status == "created"
    assert first_replay.task_snapshot is not None

    # Mark the first replay as completed so it can be replayed
    with session_scope(session_factory) as session:
        TaskRepository(session).update_status(
            task_id=first_replay.task_snapshot.task_id,
            status=TaskStatus.COMPLETED,
        )

    second_replay = service.replay_task(source_task_id=first_replay.task_snapshot.task_id)
    assert second_replay.status == "created"
    assert second_replay.task_snapshot is not None
    assert second_replay.task_snapshot.task_id != first_replay.task_snapshot.task_id

    # The second replay should point to the first replay as its source
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(second_replay.task_snapshot.task_id)
        assert task is not None
        # Should contain both IDs, newest first
        assert task.constraints.get("replayed_from") == [
            first_replay.task_snapshot.task_id,
            original_id,
        ]


def test_replay_without_overrides_preserves_original_parameters() -> None:
    """Replay with no overrides should preserve every original parameter."""
    service, session_factory = _make_service()
    source_id = _create_terminal_task(
        service,
        session_factory,
        task_text="Build the feature",
        repo_url="https://github.com/example/repo",
        branch="feature-branch",
        worker_override=WorkerType.GEMINI,
        constraints={"requires_approval": True},
        budget={"max_minutes": 10},
    )

    result = service.replay_task(source_task_id=source_id)

    assert result.status == "created"
    assert result.task_snapshot is not None
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(result.task_snapshot.task_id)
        assert task is not None
        assert task.task_text == "Build the feature"
        assert task.repo_url == "https://github.com/example/repo"
        assert task.branch == "feature-branch"
        assert task.worker_override == WorkerType.GEMINI
        assert task.constraints.get("requires_approval") is True
        assert task.budget.get("max_minutes") == 10


def test_replay_with_none_replay_request_works() -> None:
    """Passing replay_request=None should behave the same as no overrides."""
    service, session_factory = _make_service()
    source_id = _create_terminal_task(service, session_factory)

    result = service.replay_task(source_task_id=source_id, replay_request=None)

    assert result.status == "created"
    assert result.task_snapshot is not None


def test_deep_merge_is_truly_deep() -> None:
    """_deep_merge should not share mutable references between target/source and result."""
    target = {"list": [1, 2], "dict": {"a": 1}}
    source = {"list": [3], "dict": {"b": 2}}

    merged = execution_module._deep_merge(target, source)

    # Mutate the source/target and ensure merged is unaffected
    target["list"].append(4)
    target["dict"]["a"] = 99
    source["list"].append(5)
    source["dict"]["b"] = 88

    assert merged["list"] == [3]
    assert merged["dict"] == {"a": 1, "b": 2}


def test_replay_avoids_redundant_provenance() -> None:
    """Replaying from the same source multiple times should not double-count it in audit trail."""
    service, session_factory = _make_service()
    source_id = _create_terminal_task(service, session_factory)

    # First replay
    result1 = service.replay_task(source_task_id=source_id)
    assert result1.task_snapshot is not None
    with session_scope(session_factory) as session:
        task1 = TaskRepository(session).get(result1.task_snapshot.task_id)
        assert task1.constraints["replayed_from"] == [source_id]
        # Mark it completed so it can be replayed again (though we'll replay from source_id again)
        TaskRepository(session).update_status(task_id=task1.id, status=TaskStatus.COMPLETED)

    # Force the source ID back into the provenance chain manually to simulate a redundant state
    # or just replay from a task that already has it.
    # Actually, the fix is about replaying from source_id when source_id is already in the chain.

    # Suppose we have a task that already has source_id in its chain
    with session_scope(session_factory) as session:
        # Create a task that already has source_id in replayed_from
        submission = execution_module.TaskSubmission(
            task_text="Re-re-play",
            constraints={"replayed_from": [source_id, "other_id"]},
        )
        snapshot, _ = service.create_task(submission)
        TaskRepository(session).update_status(task_id=snapshot.task_id, status=TaskStatus.COMPLETED)
        branch_id = snapshot.task_id

    # Now replay FROM branch_id
    result2 = service.replay_task(source_task_id=branch_id)
    assert result2.task_snapshot is not None
    with session_scope(session_factory) as session:
        task2 = TaskRepository(session).get(result2.task_snapshot.task_id)
        # Should be [branch_id, source_id, "other_id"]
        # If we didn't filter, and we prepended branch_id, it would be fine.
        # But if we replayed from branch_id and branch_id was somehow already in the chain...
        assert task2.constraints["replayed_from"] == [branch_id, source_id, "other_id"]

    # The real test case: Replay from a task where the source_id is redundant
    # (e.g. replaying from A when A is already in A's chain - shouldn't happen
    # normally but good to guard)
    with session_scope(session_factory) as session:
        # We'll mock the ID to be self_id
        task3 = TaskRepository(session).create(
            session_id=task1.session_id,
            task_text="Recursive provenance",
            constraints={"replayed_from": ["self_id", "old_id"]},
            status=TaskStatus.COMPLETED,
            next_attempt_at=utc_now(),
        )
        task3.id = "self_id"
        session.flush()
        source_task_id = "self_id"

    result3 = service.replay_task(source_task_id=source_task_id)
    with session_scope(session_factory) as session:
        final_task = TaskRepository(session).get(result3.task_snapshot.task_id)
        # Should be ["self_id", "old_id"], not ["self_id", "self_id", "old_id"]
        assert final_task.constraints["replayed_from"] == ["self_id", "old_id"]
