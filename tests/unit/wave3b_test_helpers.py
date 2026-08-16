"""Shared fixtures and seeding helpers for Wave 3B state reduction tests."""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.base import Base, utc_now
from db.enums import TimelineEventType
from db.models import ExecutionPlanNodeAttempt, Task, User
from db.models import Session as ConversationSession
from orchestrator.execution_outcome_service import _persist_execution_outcome
from orchestrator.execution_submission_service import _load_submission_for_task
from orchestrator.node_execution import _result_digest
from orchestrator.state import (
    DecomposedTaskNode,
    DecomposedTaskPlan,
    NodeOutcome,
    OrchestratorState,
    SessionRef,
    TaskRequest,
    TaskSpec,
)
from orchestrator.temporal.activities import (
    TaskExecutionActivities,
    _serialize_temporal_task_state,
)
from repositories import (
    ExecutionPlanRepository,
    TaskTimelineRepository,
    TemporalTaskStateRepository,
    session_scope,
)
from workers import WorkerResult


def make_db() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_activities(factory: sessionmaker[Session]) -> TaskExecutionActivities:
    svc = MagicMock()
    svc.worker = MagicMock()
    svc.worker_profiles = {}
    svc.enable_worker_profiles = False
    svc.enable_independent_verifier = False
    svc.session_factory = factory
    svc.workspace_manager = None
    svc.retention_seconds = None
    svc.orchestrator_brain = None
    svc.progress_notifier = None
    svc._persist_execution_outcome = types.MethodType(_persist_execution_outcome, svc)
    svc._load_submission_for_task = types.MethodType(_load_submission_for_task, svc)

    async def _async_run_blocking(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    svc._run_blocking = _async_run_blocking
    return TaskExecutionActivities(svc)


def make_sample_state(
    task_id: str = "task-w3b-1",
    session_id: str = "session-w3b-1",
) -> OrchestratorState:
    return OrchestratorState(
        task=TaskRequest(
            task_id=task_id,
            repo_url="https://github.com/example/repo",
            task_text="Run complex refactoring task",
        ),
        session=SessionRef(
            session_id=session_id,
            user_id="user-1",
            channel="api",
            external_thread_id="thread-1",
        ),
        attempt_count=0,
    )


def seed_task_and_timeline(
    factory: sessionmaker[Session],
    task_id: str,
    *,
    decomposed_plan: DecomposedTaskPlan | None = None,
) -> None:
    with session_scope(factory) as session:
        if session.query(User).filter_by(id="user-1").first() is None:
            session.add(User(id="user-1", external_user_id="user-1", display_name="Test User"))
        if session.query(ConversationSession).filter_by(id="session-1").first() is None:
            session.add(
                ConversationSession(
                    id="session-1", user_id="user-1", channel="api", external_thread_id="t-1"
                )
            )
        session.add(
            Task(
                id=task_id,
                session_id="session-1",
                repo_url="https://github.com/example/repo",
                task_text="Task description",
                constraints={},
            )
        )
        if decomposed_plan is not None:
            TaskTimelineRepository(session).create_next_for_attempt(
                task_id=task_id,
                attempt_number=0,
                event_type=TimelineEventType.TASK_PLANNED,
                message="Decomposed",
                payload={"decomposition": decomposed_plan.model_dump(mode="json")},
            )


def seed_sql_plan_nodes(
    session: Session,
    task_id: str,
    nodes: list[DecomposedTaskNode],
) -> Any:
    plan = ExecutionPlanRepository(session).create(task_id=task_id)
    for seq, node in enumerate(nodes):
        ExecutionPlanRepository(session).add_node(
            plan_id=plan.id,
            node_id=node.node_id,
            goal=node.title,
            sequence_number=seq,
            depends_on=node.depends_on,
            task_spec=node.task_spec.model_dump(mode="json") if node.task_spec else {},
            node_kind=node.node_kind,
            aggregation_role=node.aggregation_role,
            execution_mode=node.execution_mode,
            parallel_safe=node.parallel_safe,
        )
    session.flush()
    return ExecutionPlanRepository(session).get_by_task_id(task_id)


def make_sample_node(
    node_id: str = "step-1",
    title: str = "Node 1",
    mode: str = "mutable",
    parallel_safe: bool = False,
) -> DecomposedTaskNode:
    return DecomposedTaskNode(
        node_id=node_id,
        title=title,
        depends_on=[],
        task_spec=TaskSpec(goal=title, acceptance_criteria=[]),
        node_kind="inspect" if mode == "read_only" else "implement",
        aggregation_role="context" if mode == "read_only" else "mutation",
        execution_mode=mode,
        parallel_safe=parallel_safe,
    )


def make_outcome_payload(
    node_id: str,
    status: str,
    res: WorkerResult,
    attempts: int = 1,
    key: str | None = None,
) -> tuple[NodeOutcome, dict[str, Any], str]:
    out = NodeOutcome(
        node_id=node_id,
        status=status,
        result=res,
        attempts=attempts,
        logical_activity_key=key,
    )
    payload = {
        "worker_result": res.model_dump(mode="json"),
        "node_outcome": out.model_dump(mode="json"),
    }
    digest = _result_digest(payload)
    return out, payload, digest


def add_attempt(
    session: Session,
    plan_node_id: str,
    attempt_number: int,
    key: str,
    status: str,
    payload: dict[str, Any],
    digest: str,
) -> None:
    session.add(
        ExecutionPlanNodeAttempt(
            id=f"att-{plan_node_id}-{attempt_number}",
            plan_node_id=plan_node_id,
            attempt_number=attempt_number,
            started_at=utc_now(),
            finished_at=utc_now(),
            status=status,
            effective_input_summary={},
            effective_input_digest=f"d-{attempt_number}",
            logical_activity_key=key,
            result_digest=digest,
            result_payload=payload,
        )
    )


def seed_snapshot(
    session: Session,
    task_id: str,
    decomposed_plan: DecomposedTaskPlan | None = None,
    raw_dict: dict[str, Any] | None = None,
) -> None:
    if raw_dict is not None:
        TemporalTaskStateRepository(session).upsert(task_id=task_id, state=raw_dict)
    else:
        state = make_sample_state(task_id=task_id)
        state.decomposed_plan = decomposed_plan
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id, state=_serialize_temporal_task_state(state)
        )
