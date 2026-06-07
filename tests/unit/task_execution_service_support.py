# ruff: noqa: F401
"""Shared fixtures and helpers for task execution service tests."""

from __future__ import annotations

import asyncio
import builtins
import logging
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from db.base import Base, utc_now
from db.enums import (
    ArtifactType,
    HumanInteractionStatus,
    HumanInteractionType,
    TaskStatus,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import HumanInteraction, Task, User
from db.models import Session as ConversationSession
from orchestrator import (
    ApprovalCheckpoint,
    MemoryContext,
    OrchestratorState,
    RouteDecision,
    SessionRef,
    TaskRequest,
    WorkerDispatch,
    WorkerResult,
)
from orchestrator import execution as execution_module
from orchestrator import execution_policy as execution_policy_module
from orchestrator import execution_tracing as execution_tracing_module
from orchestrator.brain import RuleBasedOrchestratorBrain
from repositories import (
    ArtifactRepository,
    HumanInteractionRepository,
    InboundDeliveryRepository,
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    SessionRepository,
    SessionStateRepository,
    TaskRepository,
    UserRepository,
    WorkerRunRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import ArtifactReference, ReviewFinding, ReviewResult, Worker, WorkerRequest


class _StaticWorker(Worker):
    """Minimal worker double used to initialize the service."""

    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(status="success", summary=f"stubbed: {request.task_text}")


class _FakeGraph:
    """Graph double that records invocations and returns a valid final state."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ainvoke(self, payload: dict[str, object], **kwargs: object) -> dict[str, object]:
        self.calls.append(payload)
        session = SessionRef.model_validate(payload["session"])
        task = TaskRequest.model_validate(payload["task"])
        return OrchestratorState(
            current_step="persist_memory",
            session=session,
            task=task,
            normalized_task_text=task.task_text,
            task_kind="implementation",
            memory=MemoryContext(),
            route=RouteDecision(
                chosen_worker="codex",
                route_reason="cheap_mechanical_change",
                override_applied=False,
            ),
            approval=ApprovalCheckpoint(),
            dispatch=WorkerDispatch(worker_type="codex"),
            result=WorkerResult(
                status="success",
                summary="fake graph completed",
            ),
            progress_updates=["task ingested", "worker result received"],
        ).model_dump(mode="json")


class _RecordingProgressNotifier:
    """Capture progress events emitted by the task execution service."""

    def __init__(self) -> None:
        self.events: list[execution_module.ProgressEvent] = []

    async def notify(
        self,
        *,
        submission: execution_module.TaskSubmission,
        event: execution_module.ProgressEvent,
    ) -> None:
        self.events.append(event)


def _build_state(
    *,
    result: WorkerResult | None = None,
    chosen_worker: str | None = "codex",
    dispatch_worker_type: str | None = "codex",
    approval_required: bool = False,
    approval_status: str = "pending",
) -> OrchestratorState:
    return OrchestratorState(
        current_step="persist_memory",
        session=SessionRef(
            session_id="session-1",
            user_id="user-1",
            channel="http",
            external_thread_id="thread-1",
        ),
        task=TaskRequest(task_text="Run the task"),
        normalized_task_text="Run the task",
        task_kind="implementation",
        memory=MemoryContext(),
        route=RouteDecision(
            chosen_worker=chosen_worker,
            route_reason="cheap_mechanical_change" if chosen_worker else None,
            override_applied=False,
        ),
        approval=ApprovalCheckpoint(required=approval_required, status=approval_status),
        dispatch=WorkerDispatch(worker_type=dispatch_worker_type),
        result=result,
    )


def _make_task_service() -> tuple[execution_module.TaskExecutionService, object]:
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


__all__ = [
    "_StaticWorker",
    "_FakeGraph",
    "_RecordingProgressNotifier",
    "_build_state",
    "_make_task_service",
    *[
        name
        for name in globals()
        if not name.startswith("__")
        and name
        not in {
            "_StaticWorker",
            "_FakeGraph",
            "_RecordingProgressNotifier",
            "_build_state",
            "_make_task_service",
        }
    ],
]
