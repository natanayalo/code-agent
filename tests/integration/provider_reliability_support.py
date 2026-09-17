"""Shared synthetic fixtures and helpers for provider reliability extractor tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from db.base import Base
from db.enums import (
    HumanInteractionType,
    OrchestrationRuntime,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import (
    HumanInteraction,
    Task,
    TaskTimelineEvent,
    WorkerRun,
)
from repositories import create_engine_from_url

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def _add_worker_run(
    task: Task,
    profile: str,
    mode: WorkerRuntimeMode,
    runtime: OrchestrationRuntime,
    status: TaskStatus,
    created_at: datetime,
    updated_at: datetime,
    failure_kind: str | None,
    budget: dict | None,
) -> None:
    """Add a worker run attempt to the task."""
    run_status = (
        WorkerRunStatus.SUCCESS if status == TaskStatus.COMPLETED else WorkerRunStatus.FAILURE
    )
    if status == TaskStatus.COMPLETED:
        verifier_outcome = {"status": "passed"}
    else:
        verifier_outcome = {"status": "failed", "failure_kind": failure_kind or "test_failure"}
    task.worker_runs.append(
        WorkerRun(
            worker_type=WorkerType.CODEX,
            worker_profile=profile,
            runtime_mode=mode,
            orchestration_runtime=runtime,
            started_at=created_at,
            finished_at=updated_at,
            status=run_status,
            verifier_outcome=verifier_outcome,
            budget_usage=budget or {"tokens": 100},
        )
    )


def _add_timeline_events(
    task: Task,
    status: TaskStatus,
    created_at: datetime,
    updated_at: datetime,
    inconsistent: bool,
) -> None:
    """Attach ingested and terminal timeline events."""
    status_to_event = {
        TaskStatus.COMPLETED: TimelineEventType.TASK_COMPLETED,
        TaskStatus.FAILED: TimelineEventType.TASK_FAILED,
        TaskStatus.CANCELLED: TimelineEventType.TASK_CANCELLED,
    }
    terminal_type = (
        TimelineEventType.TASK_FAILED
        if inconsistent
        else status_to_event.get(status, TimelineEventType.WORKER_DISPATCHED)
    )

    task.timeline_events.append(
        TaskTimelineEvent(
            attempt_number=0,
            sequence_number=0,
            event_type=TimelineEventType.TASK_INGESTED,
            created_at=created_at,
        )
    )
    task.timeline_events.append(
        TaskTimelineEvent(
            attempt_number=0,
            sequence_number=1,
            event_type=terminal_type,
            created_at=updated_at,
        )
    )


def _add_clarifications(task: Task, count: int, created_at: datetime) -> None:
    """Attach human clarification interactions."""
    for q_idx in range(count):
        task.human_interactions.append(
            HumanInteraction(
                interaction_type=HumanInteractionType.CLARIFICATION,
                summary=f"private clarification question {q_idx}",
                data={"questions": ["Clarify?"]},
                created_at=created_at,
            )
        )


def create_test_task(
    *,
    task_id: str,
    status: TaskStatus,
    task_class: str = "feature",
    profile: str = "codex-native-executor",
    runtime: OrchestrationRuntime = OrchestrationRuntime.TEMPORAL,
    mode: WorkerRuntimeMode = WorkerRuntimeMode.NATIVE_AGENT,
    created_at: datetime = NOW - timedelta(days=10),
    updated_at: datetime = NOW - timedelta(days=10, minutes=-5),
    with_spec: bool = True,
    with_profile: bool = True,
    inconsistent_timeline: bool = False,
    verification_cmds: list[str] | None = None,
    delivery_mode: str = "workspace",
    has_override: bool = False,
    failure_kind: str | None = None,
    budget: dict | None = None,
    verifier_repair: bool = False,
    clarifications: int = 0,
) -> Task:
    """Helper to construct a synthetic task with complete graph state."""
    spec = None
    if with_spec:
        allowed = [] if task_class == "scout" else ["modify_workspace_files"]
        spec = {
            "task_type": task_class,
            "allowed_actions": allowed,
            "verification_commands": verification_cmds or [],
            "delivery_mode": delivery_mode,
        }

    constraints: dict = {}
    if verifier_repair:
        constraints["independent_verifier_repair_passes_used"] = 1
    if has_override:
        constraints["worker_override"] = "codex"

    task = Task(
        session_id="synthetic-session-id",
        task_text=f"private text for {task_id}",
        repo_url="https://github.invalid/private/repo",
        branch="feature-private",
        status=status,
        chosen_worker=WorkerType.CODEX,
        chosen_profile=profile if with_profile else None,
        runtime_mode=mode,
        orchestration_runtime=runtime,
        task_spec=spec,
        constraints=constraints,
        worker_override=WorkerType.CODEX if has_override else None,
        created_at=created_at,
        updated_at=updated_at,
    )

    _add_worker_run(
        task,
        profile if with_profile else None,
        mode,
        runtime,
        status,
        created_at,
        updated_at,
        failure_kind,
        budget,
    )
    _add_timeline_events(task, status, created_at, updated_at, inconsistent_timeline)
    _add_clarifications(task, clarifications, created_at)
    return task


def seed_valid_tasks() -> list[Task]:
    """Generate 20 valid tasks for codex and antigravity."""
    tasks = [
        create_test_task(
            task_id=f"codex-success-{i}",
            status=TaskStatus.COMPLETED,
            task_class="feature",
            profile="codex-native-executor",
            has_override=(i == 0),
            verifier_repair=(i == 1),
            clarifications=(1 if i == 2 else 0),
        )
        for i in range(10)
    ]
    tasks.extend(
        create_test_task(
            task_id=f"antigravity-success-{i}",
            status=TaskStatus.COMPLETED,
            task_class="feature",
            profile="antigravity-native-executor",
        )
        for i in range(8)
    )
    tasks.extend(
        create_test_task(
            task_id=f"antigravity-failed-{i}",
            status=TaskStatus.FAILED,
            task_class="feature",
            profile="antigravity-native-executor",
            failure_kind="compile_error",
        )
        for i in range(2)
    )
    return tasks


def seed_excluded_tasks() -> list[Task]:
    """Generate excluded task variants covering all exclusion reasons."""
    return [
        create_test_task(task_id="cancelled-1", status=TaskStatus.CANCELLED),
        create_test_task(task_id="incomplete-1", status=TaskStatus.IN_PROGRESS),
        create_test_task(
            task_id="legacy-runtime",
            status=TaskStatus.COMPLETED,
            runtime=OrchestrationRuntime.LEGACY,
        ),
        create_test_task(
            task_id="tool-loop-mode",
            status=TaskStatus.COMPLETED,
            mode=WorkerRuntimeMode.TOOL_LOOP,
        ),
        create_test_task(
            task_id="old-task",
            status=TaskStatus.COMPLETED,
            created_at=NOW - timedelta(days=120),
            updated_at=NOW - timedelta(days=119),
        ),
        create_test_task(task_id="missing-spec", status=TaskStatus.COMPLETED, with_spec=False),
        create_test_task(
            task_id="missing-profile", status=TaskStatus.COMPLETED, with_profile=False
        ),
        create_test_task(
            task_id="inconsistent-timeline",
            status=TaskStatus.COMPLETED,
            inconsistent_timeline=True,
        ),
    ]


def seed_test_database(tmp_path: Path) -> str:
    """Seed synthetic tasks covering successes, failures, exclusions, and edge cases."""
    db_path = tmp_path / "synthetic_m29.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    all_tasks = seed_valid_tasks() + seed_excluded_tasks()

    with Session(engine) as session:
        for t in all_tasks:
            session.add(t)
        session.commit()
    engine.dispose()
    return db_url


__all__ = [
    "NOW",
    "create_test_task",
    "seed_excluded_tasks",
    "seed_test_database",
    "seed_valid_tasks",
]
