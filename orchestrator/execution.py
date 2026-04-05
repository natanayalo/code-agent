"""Execution-path persistence service for the T-044 HTTP vertical slice."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from anyio import to_thread
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from db.base import utc_now
from db.enums import ArtifactType, TaskStatus, WorkerRunStatus, WorkerType
from db.models import (
    Session as ConversationSession,
)
from db.models import User
from orchestrator.graph import build_orchestrator_graph
from orchestrator.state import OrchestratorState, SessionRef
from repositories import (
    ArtifactRepository,
    SessionRepository,
    TaskRepository,
    UserRepository,
    WorkerRunRepository,
    session_scope,
)
from workers import ArtifactReference, Worker

logger = logging.getLogger(__name__)


class ExecutionModel(BaseModel):
    """Base model for task-execution service payloads."""

    model_config = ConfigDict(extra="forbid")


class SubmissionSession(ExecutionModel):
    """Caller identity and thread metadata for a submitted task."""

    channel: str = Field(default="http", min_length=1)
    external_user_id: str = Field(default="http:anonymous", min_length=1)
    external_thread_id: str = Field(default="http-default", min_length=1)
    display_name: str | None = None


class TaskSubmission(ExecutionModel):
    """HTTP payload accepted by the minimal task-submission endpoint."""

    task_text: str = Field(min_length=1)
    repo_url: str | None = None
    branch: str | None = None
    priority: int = Field(default=0, ge=0)
    worker_override: WorkerType | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    session: SubmissionSession = Field(default_factory=SubmissionSession)


class ArtifactSnapshot(ExecutionModel):
    """One persisted artifact returned by the task status API."""

    artifact_id: str
    artifact_type: str
    name: str
    uri: str
    artifact_metadata: dict[str, Any] | None = None


class WorkerRunSnapshot(ExecutionModel):
    """The latest persisted worker run associated with a task."""

    run_id: str
    worker_type: str
    workspace_id: str | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    summary: str | None = None
    commands_run: list[dict[str, Any]] = Field(default_factory=list)
    files_changed_count: int = 0
    artifact_index: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[ArtifactSnapshot] = Field(default_factory=list)


class TaskSnapshot(ExecutionModel):
    """The persisted task view returned by POST/GET task endpoints."""

    task_id: str
    session_id: str
    status: str
    task_text: str
    repo_url: str | None = None
    branch: str | None = None
    priority: int
    chosen_worker: str | None = None
    route_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    latest_run: WorkerRunSnapshot | None = None


@dataclass(frozen=True)
class _PersistedTaskContext:
    """The DB-backed task/session identifiers needed during execution."""

    user_id: str
    session_id: str
    channel: str
    external_thread_id: str
    task_id: str


def _enum_value(value: object | None) -> str | None:
    """Normalize enum-backed ORM values into plain strings."""
    if value is None:
        return None
    member_value = getattr(value, "value", None)
    if isinstance(member_value, str):
        return member_value
    return str(value)


def _task_status_from_result(state: OrchestratorState) -> str:
    """Map the final orchestrator result into a persisted task status."""
    if state.result is None:
        return TaskStatus.FAILED.value
    if state.result.status == "success":
        return TaskStatus.COMPLETED.value
    return TaskStatus.FAILED.value


def _worker_run_status_from_result(state: OrchestratorState) -> str:
    """Map the final worker result into a persisted worker-run status."""
    if state.result is None:
        return WorkerRunStatus.ERROR.value
    if state.result.status == "success":
        return WorkerRunStatus.SUCCESS.value
    if state.result.status == "failure":
        return WorkerRunStatus.FAILURE.value
    return WorkerRunStatus.ERROR.value


def _workspace_id_from_artifacts(artifacts: list[ArtifactReference]) -> str | None:
    """Infer the workspace id from the retained workspace artifact path."""
    for artifact in artifacts:
        if artifact.artifact_type == ArtifactType.WORKSPACE.value or artifact.name == "workspace":
            parsed_uri = urlparse(artifact.uri)
            candidate = ""
            if parsed_uri.scheme and parsed_uri.path:
                candidate = Path(unquote(parsed_uri.path)).name.strip()
            elif parsed_uri.scheme and parsed_uri.netloc:
                candidate = parsed_uri.netloc.strip()
            else:
                candidate = Path(unquote(artifact.uri)).name.strip()
            if candidate:
                return candidate
    return None


def _artifact_type_for_persistence(artifact: ArtifactReference) -> str | None:
    """Return a DB-supported artifact type for the emitted artifact."""
    if artifact.artifact_type is None:
        return None
    try:
        return ArtifactType(artifact.artifact_type).value
    except ValueError:
        logger.warning(
            "Skipping unsupported artifact type during execution-path persistence",
            extra={"artifact_name": artifact.name, "artifact_type": artifact.artifact_type},
        )
        return None


class TaskExecutionService:
    """Submit tasks through the orchestrator and persist execution-path state."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        worker: Worker,
    ) -> None:
        self.session_factory = session_factory
        self.worker = worker
        self.graph = build_orchestrator_graph(worker=worker)

    def create_task(self, submission: TaskSubmission) -> tuple[TaskSnapshot, _PersistedTaskContext]:
        """Persist a new task request and return the initial pollable snapshot."""
        persisted = self._persist_submission(submission, status=TaskStatus.PENDING)
        task_snapshot = self.get_task(persisted.task_id)
        if task_snapshot is None:
            raise RuntimeError(f"Persisted task '{persisted.task_id}' could not be reloaded.")
        return task_snapshot, persisted

    async def submit_task(
        self,
        submission: TaskSubmission,
        persisted: _PersistedTaskContext,
    ) -> None:
        """Execute one previously persisted task request in the background."""
        await self._run_blocking(self._mark_task_in_progress, task_id=persisted.task_id)
        started_at = utc_now()
        logger.info(
            "Starting execution-path task run",
            extra={
                "session_id": persisted.session_id,
                "task_id": persisted.task_id,
                "chosen_worker": None,
                "route_reason": None,
                "workspace_id": None,
                "start_timestamp": started_at.isoformat(),
            },
        )

        try:
            state = await self._run_orchestrator(submission, persisted)
            finished_at = utc_now()
            await self._run_blocking(
                self._persist_execution_outcome,
                task_id=persisted.task_id,
                state=state,
                started_at=started_at,
                finished_at=finished_at,
            )
        except Exception:
            logger.exception(
                "Task execution failed before the final outcome was fully persisted",
                extra={
                    "session_id": persisted.session_id,
                    "task_id": persisted.task_id,
                },
            )
            await self._run_blocking(self._mark_task_failed, task_id=persisted.task_id)
            task_snapshot = await self._run_blocking(self.get_task, persisted.task_id)
            if task_snapshot is None:
                logger.error(
                    "Failed to reload task snapshot after marking a background task as failed",
                    extra={
                        "session_id": persisted.session_id,
                        "task_id": persisted.task_id,
                    },
                )
                return None
            self._log_task_outcome(task_snapshot)
            return None

        task_snapshot = await self._run_blocking(self.get_task, persisted.task_id)
        if task_snapshot is None:
            raise RuntimeError(f"Persisted task '{persisted.task_id}' could not be reloaded.")
        self._log_task_outcome(task_snapshot)
        return None

    def get_task(self, task_id: str) -> TaskSnapshot | None:
        """Load the current persisted task state and its latest worker run."""
        with session_scope(self.session_factory) as session:
            task_repo = TaskRepository(session)
            worker_run_repo = WorkerRunRepository(session)
            artifact_repo = ArtifactRepository(session)

            task = task_repo.get(task_id)
            if task is None:
                return None

            latest_run_snapshot: WorkerRunSnapshot | None = None
            worker_runs = worker_run_repo.list_by_task(task.id)
            if worker_runs:
                latest_run = worker_runs[-1]
                artifacts = artifact_repo.list_by_run(latest_run.id)
                latest_run_snapshot = WorkerRunSnapshot(
                    run_id=latest_run.id,
                    worker_type=_enum_value(latest_run.worker_type) or "unknown",
                    workspace_id=latest_run.workspace_id,
                    status=_enum_value(latest_run.status) or WorkerRunStatus.ERROR.value,
                    started_at=latest_run.started_at,
                    finished_at=latest_run.finished_at,
                    summary=latest_run.summary,
                    commands_run=list(latest_run.commands_run or []),
                    files_changed_count=latest_run.files_changed_count,
                    artifact_index=list(latest_run.artifact_index or []),
                    artifacts=[
                        ArtifactSnapshot(
                            artifact_id=artifact.id,
                            artifact_type=_enum_value(artifact.artifact_type)
                            or ArtifactType.RESULT_SUMMARY.value,
                            name=artifact.name,
                            uri=artifact.uri,
                            artifact_metadata=artifact.artifact_metadata,
                        )
                        for artifact in artifacts
                    ],
                )

            return TaskSnapshot(
                task_id=task.id,
                session_id=task.session_id,
                status=_enum_value(task.status) or TaskStatus.FAILED.value,
                task_text=task.task_text,
                repo_url=task.repo_url,
                branch=task.branch,
                priority=task.priority,
                chosen_worker=_enum_value(task.chosen_worker),
                route_reason=task.route_reason,
                created_at=task.created_at,
                updated_at=task.updated_at,
                latest_run=latest_run_snapshot,
            )

    def _persist_submission(
        self,
        submission: TaskSubmission,
        *,
        status: TaskStatus,
    ) -> _PersistedTaskContext:
        """Create or restore the session scaffolding for a submitted task."""
        now = utc_now()
        with session_scope(self.session_factory) as session:
            user_repo = UserRepository(session)
            session_repo = SessionRepository(session)
            task_repo = TaskRepository(session)

            user = user_repo.get_by_external_user_id(submission.session.external_user_id)
            if user is None:
                user = self._create_or_get_user(
                    session,
                    user_repo,
                    external_user_id=submission.session.external_user_id,
                    display_name=submission.session.display_name,
                )

            conversation_session = session_repo.get_by_channel_thread(
                channel=submission.session.channel,
                external_thread_id=submission.session.external_thread_id,
            )
            if conversation_session is None:
                conversation_session = self._create_or_get_session(
                    session,
                    session_repo,
                    user_id=user.id,
                    channel=submission.session.channel,
                    external_thread_id=submission.session.external_thread_id,
                    last_seen_at=now,
                )
            session_repo.touch(session_id=conversation_session.id, seen_at=now)

            task = task_repo.create(
                session_id=conversation_session.id,
                task_text=submission.task_text,
                repo_url=submission.repo_url,
                branch=submission.branch,
                status=status,
                priority=submission.priority,
            )
            session_repo.set_active_task(
                session_id=conversation_session.id,
                active_task_id=task.id,
            )
            return _PersistedTaskContext(
                user_id=user.id,
                session_id=conversation_session.id,
                channel=conversation_session.channel,
                external_thread_id=conversation_session.external_thread_id,
                task_id=task.id,
            )

    async def _run_orchestrator(
        self,
        submission: TaskSubmission,
        persisted: _PersistedTaskContext,
    ) -> OrchestratorState:
        """Execute the orchestrator graph for one submitted task."""
        raw_output = await self.graph.ainvoke(
            {
                "session": SessionRef(
                    session_id=persisted.session_id,
                    user_id=persisted.user_id,
                    channel=persisted.channel,
                    external_thread_id=persisted.external_thread_id,
                    active_task_id=persisted.task_id,
                    status="active",
                ).model_dump(),
                "task": {
                    "task_id": persisted.task_id,
                    "task_text": submission.task_text,
                    "repo_url": submission.repo_url,
                    "branch": submission.branch,
                    "priority": submission.priority,
                    "worker_override": (
                        submission.worker_override.value
                        if submission.worker_override is not None
                        else None
                    ),
                    "constraints": dict(submission.constraints),
                    "budget": dict(submission.budget),
                },
            }
        )
        return OrchestratorState.model_validate(raw_output)

    def _mark_task_in_progress(self, *, task_id: str) -> None:
        """Mark a queued task as in progress when background execution begins."""
        with session_scope(self.session_factory) as session:
            task_repo = TaskRepository(session)
            task_repo.update_status(task_id=task_id, status=TaskStatus.IN_PROGRESS)

    def _mark_task_failed(self, *, task_id: str) -> None:
        """Persist a failed task status after background execution crashes."""
        with session_scope(self.session_factory) as session:
            task_repo = TaskRepository(session)
            task_repo.update_status(task_id=task_id, status=TaskStatus.FAILED)

    async def _run_blocking(
        self,
        func: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run a synchronous persistence operation in a worker thread."""
        return await to_thread.run_sync(partial(func, *args, **kwargs))

    def _create_or_get_user(
        self,
        session: Session,
        user_repo: UserRepository,
        *,
        external_user_id: str,
        display_name: str | None,
    ) -> User:
        """Create a user or recover the concurrently inserted row."""
        try:
            with session.begin_nested():
                return user_repo.create(
                    external_user_id=external_user_id,
                    display_name=display_name,
                )
        except IntegrityError:
            existing_user = user_repo.get_by_external_user_id(external_user_id)
            if existing_user is None:
                raise
            return existing_user

    def _create_or_get_session(
        self,
        session: Session,
        session_repo: SessionRepository,
        *,
        user_id: str,
        channel: str,
        external_thread_id: str,
        last_seen_at: datetime,
    ) -> ConversationSession:
        """Create a session or recover the concurrently inserted row."""
        try:
            with session.begin_nested():
                return session_repo.create(
                    user_id=user_id,
                    channel=channel,
                    external_thread_id=external_thread_id,
                    last_seen_at=last_seen_at,
                )
        except IntegrityError:
            existing_session = session_repo.get_by_channel_thread(
                channel=channel,
                external_thread_id=external_thread_id,
            )
            if existing_session is None:
                raise
            return existing_session

    def _persist_execution_outcome(
        self,
        *,
        task_id: str,
        state: OrchestratorState,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        """Persist route, task status, worker-run metadata, and artifacts."""
        with session_scope(self.session_factory) as session:
            task_repo = TaskRepository(session)
            worker_run_repo = WorkerRunRepository(session)
            artifact_repo = ArtifactRepository(session)

            if state.route.chosen_worker is not None and state.route.route_reason is not None:
                task_repo.set_route(
                    task_id=task_id,
                    chosen_worker=state.route.chosen_worker,
                    route_reason=state.route.route_reason,
                )

            task_repo.update_status(task_id=task_id, status=_task_status_from_result(state))

            if state.dispatch.worker_type is None:
                return

            result = state.result
            artifacts = result.artifacts if result is not None else []
            artifact_index = [artifact.model_dump(mode="json") for artifact in artifacts]
            worker_run = worker_run_repo.create(
                task_id=task_id,
                worker_type=state.dispatch.worker_type,
                workspace_id=_workspace_id_from_artifacts(artifacts),
                started_at=started_at,
                finished_at=finished_at,
                status=_worker_run_status_from_result(state),
                summary=result.summary if result is not None else "Worker did not return a result.",
                commands_run=[
                    command.model_dump(mode="json")
                    for command in (result.commands_run if result is not None else [])
                ],
                files_changed_count=len(result.files_changed) if result is not None else 0,
                files_changed=result.files_changed if result is not None else [],
                artifact_index=artifact_index,
            )

            for artifact in artifacts:
                artifact_type = _artifact_type_for_persistence(artifact)
                if artifact_type is None:
                    continue
                artifact_repo.create(
                    run_id=worker_run.id,
                    artifact_type=artifact_type,
                    name=artifact.name,
                    uri=artifact.uri,
                )

    def _log_task_outcome(self, task_snapshot: TaskSnapshot) -> None:
        """Emit the structured task-run log required for execution-path tracing."""
        latest_run = task_snapshot.latest_run
        logger.info(
            "Persisted execution-path task run",
            extra={
                "session_id": task_snapshot.session_id,
                "task_id": task_snapshot.task_id,
                "chosen_worker": task_snapshot.chosen_worker,
                "route_reason": task_snapshot.route_reason,
                "workspace_id": latest_run.workspace_id if latest_run is not None else None,
                "start_timestamp": latest_run.started_at.isoformat() if latest_run else None,
                "end_timestamp": latest_run.finished_at.isoformat()
                if latest_run and latest_run.finished_at is not None
                else None,
                "final_status": task_snapshot.status,
                "changed_files_count": latest_run.files_changed_count if latest_run else 0,
                "artifact_list": [
                    {
                        "name": artifact.name,
                        "uri": artifact.uri,
                        "artifact_type": artifact.artifact_type,
                    }
                    for artifact in (latest_run.artifacts if latest_run is not None else [])
                ],
            },
        )
