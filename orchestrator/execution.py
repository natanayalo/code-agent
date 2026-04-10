"""Execution-path persistence service for the T-044 HTTP vertical slice."""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlparse

from anyio import to_thread
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from db.base import utc_now
from db.enums import ArtifactType, TaskStatus, WorkerRunStatus, WorkerType
from db.models import (
    Session as ConversationSession,
)
from db.models import (
    User,
)
from orchestrator.graph import build_orchestrator_graph
from orchestrator.state import OrchestratorState, SessionRef
from repositories import (
    ArtifactRepository,
    InboundDeliveryRepository,
    SessionRepository,
    SessionStateRepository,
    TaskRepository,
    UserRepository,
    WorkerRunRepository,
    session_scope,
)
from workers import ArtifactReference, Worker

logger = logging.getLogger(__name__)

_CALLBACK_RESOLUTION_TIMEOUT_SECONDS = 2.0
_CALLBACK_DNS_EXECUTOR_MAX_WORKERS = 4
_callback_dns_executor: ThreadPoolExecutor | None = None
_callback_dns_executor_lock = Lock()


class ExecutionModel(BaseModel):
    """Base model for task-execution service payloads."""

    model_config = ConfigDict(extra="forbid")


class SubmissionSession(ExecutionModel):
    """Caller identity and thread metadata for a submitted task."""

    channel: str = Field(default="http", min_length=1)
    external_user_id: str = Field(default="http:anonymous", min_length=1)
    external_thread_id: str = Field(default="http-default", min_length=1)
    display_name: str | None = None


def _is_unsafe_callback_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether a resolved callback destination is local-only or otherwise unsafe."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped

    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _lookup_callback_hostname_records(hostname: str, port: int) -> list[tuple]:
    """Resolve a hostname through the system resolver using TCP-oriented address hints."""
    return socket.getaddrinfo(
        hostname,
        port,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )


def _get_callback_dns_executor() -> ThreadPoolExecutor:
    """Return the shared executor used for bounded callback DNS resolution."""
    global _callback_dns_executor
    with _callback_dns_executor_lock:
        if _callback_dns_executor is None:
            _callback_dns_executor = ThreadPoolExecutor(
                max_workers=_CALLBACK_DNS_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="callback-dns",
            )
        return _callback_dns_executor


def shutdown_callback_dns_executor() -> None:
    """Shut down the shared callback DNS executor for app/test teardown."""
    global _callback_dns_executor
    with _callback_dns_executor_lock:
        executor = _callback_dns_executor
        _callback_dns_executor = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def _resolve_callback_hostname(
    hostname: str,
    *,
    port: int,
    timeout_seconds: float = _CALLBACK_RESOLUTION_TIMEOUT_SECONDS,
) -> list[str]:
    """Resolve a callback hostname into concrete destination IP addresses."""
    executor = _get_callback_dns_executor()
    try:
        future = executor.submit(_lookup_callback_hostname_records, hostname, port)
        records = future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        future.cancel()
        raise ValueError("callback_url hostname resolution timed out.") from exc
    except socket.gaierror as exc:
        raise ValueError("callback_url hostname could not be resolved.") from exc
    except FutureCancelledError as exc:
        raise ValueError("callback_url hostname resolution was cancelled.") from exc

    resolved_addresses: list[str] = []
    for _, _, _, _, sockaddr in records:
        if not sockaddr:
            continue
        candidate = sockaddr[0].strip()
        if candidate:
            resolved_addresses.append(candidate)

    if not resolved_addresses:
        raise ValueError("callback_url hostname did not resolve to any addresses.")
    return resolved_addresses


def _validate_callback_url(value: str | None) -> str | None:
    """Reject callback targets that are malformed or obviously unsafe for outbound POSTs."""
    if value is None:
        return None

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("callback_url must use http or https.")
    if not parsed.netloc or parsed.hostname is None:
        raise ValueError("callback_url must be an absolute URL with a hostname.")

    hostname = parsed.hostname.strip().lower()
    if hostname == "localhost":
        raise ValueError("callback_url must not target localhost.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("callback_url must include a valid port.") from exc
    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    try:
        ipaddress.ip_address(hostname)
        resolved_addresses = [hostname]
    except ValueError:
        resolved_addresses = _resolve_callback_hostname(hostname, port=port)

    for resolved_address in resolved_addresses:
        host_ip = ipaddress.ip_address(resolved_address)
        if _is_unsafe_callback_address(host_ip):
            raise ValueError("callback_url must not target a private or local address.")
    return value


class TaskSubmission(ExecutionModel):
    """HTTP payload accepted by the minimal task-submission endpoint."""

    task_text: str = Field(min_length=1)
    repo_url: str | None = None
    branch: str | None = None
    priority: int = Field(default=0, ge=0)
    worker_override: WorkerType | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    callback_url: str | None = Field(default=None, max_length=2048)
    session: SubmissionSession = Field(default_factory=SubmissionSession)

    @field_validator("callback_url")
    @classmethod
    def validate_callback_url(cls, value: str | None) -> str | None:
        """Ensure callback URLs are safe for outbound progress delivery."""
        return _validate_callback_url(value)


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
    session_id: str | None = None
    worker_type: str
    workspace_id: str | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    summary: str | None = None
    requested_permission: str | None = None
    budget_usage: dict[str, Any] | None = None
    verifier_outcome: dict[str, Any] | None = None
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
class DeliveryKey:
    """A caller-supplied idempotency key for one inbound delivery."""

    channel: str
    delivery_id: str


@dataclass(frozen=True)
class _PersistedTaskContext:
    """The DB-backed task/session identifiers needed during execution."""

    user_id: str
    session_id: str
    channel: str
    external_thread_id: str
    task_id: str


@dataclass(frozen=True)
class CreateTaskOutcome:
    """Result of persisting or deduping a submitted task."""

    task_snapshot: TaskSnapshot
    persisted: _PersistedTaskContext | None
    duplicate: bool = False


@dataclass(frozen=True)
class ProgressEvent:
    """A task lifecycle update emitted by the execution service."""

    phase: Literal["started", "running", "completed", "failed"]
    task_id: str
    session_id: str
    channel: str
    external_thread_id: str
    task_text: str
    summary: str | None = None


class ProgressNotifier(Protocol):
    """Async sink for task lifecycle updates."""

    async def notify(self, *, submission: TaskSubmission, event: ProgressEvent) -> None:
        """Deliver one task lifecycle event."""


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


def _serialize_verification_report(report: object | None) -> dict[str, Any] | None:
    """Normalize verification state from either a Pydantic model or a raw mapping."""
    if report is None:
        return None
    if hasattr(report, "model_dump"):
        return report.model_dump(mode="json")
    if isinstance(report, Mapping):
        return dict(report)
    raise TypeError(f"Unsupported verification report type: {type(report).__name__}")


class TaskExecutionService:
    """Submit tasks through the orchestrator and persist execution-path state."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        worker: Worker,
        gemini_worker: Worker | None = None,
        progress_notifier: ProgressNotifier | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.worker = worker
        self.gemini_worker = gemini_worker
        self.progress_notifier = progress_notifier
        self.graph = build_orchestrator_graph(worker=worker, gemini_worker=gemini_worker)

    def create_task(self, submission: TaskSubmission) -> tuple[TaskSnapshot, _PersistedTaskContext]:
        """Persist a new task request and return the initial pollable snapshot."""
        outcome = self.create_task_outcome(submission)
        if outcome.persisted is None:
            raise RuntimeError("Expected a fresh task, but a duplicate delivery was returned.")
        return outcome.task_snapshot, outcome.persisted

    def create_task_outcome(
        self,
        submission: TaskSubmission,
        *,
        delivery_key: DeliveryKey | None = None,
    ) -> CreateTaskOutcome:
        """Persist a task request or return the previously created task for a duplicate delivery."""
        if delivery_key is not None and delivery_key.channel != submission.session.channel:
            raise ValueError(
                "delivery_key.channel must match submission.session.channel for dedupe."
            )
        persisted, duplicate_task_id = self._persist_submission(
            submission,
            status=TaskStatus.PENDING,
            delivery_key=delivery_key,
        )
        task_id = duplicate_task_id or (persisted.task_id if persisted is not None else None)
        if task_id is None:
            raise RuntimeError("Task persistence did not produce a task id.")

        task_snapshot = self.get_task(task_id)
        if task_snapshot is None:
            raise RuntimeError(f"Persisted task '{task_id}' could not be reloaded.")
        return CreateTaskOutcome(
            task_snapshot=task_snapshot,
            persisted=persisted,
            duplicate=duplicate_task_id is not None,
        )

    async def submit_task(
        self,
        submission: TaskSubmission,
        persisted: _PersistedTaskContext,
    ) -> None:
        """Execute one previously persisted task request in the background."""
        await self._run_blocking(self._mark_task_in_progress, task_id=persisted.task_id)
        await self._emit_progress(
            submission,
            persisted,
            phase="started",
        )
        await self._emit_progress(
            submission,
            persisted,
            phase="running",
        )
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
                await self._emit_progress(
                    submission,
                    persisted,
                    phase="failed",
                    summary="Task execution failed and the final snapshot could not be reloaded.",
                )
                return None
            self._log_task_outcome(task_snapshot)
            await self._emit_progress(
                submission,
                persisted,
                phase="failed",
                summary=self._task_summary(task_snapshot),
            )
            return None

        task_snapshot = await self._run_blocking(self.get_task, persisted.task_id)
        if task_snapshot is None:
            raise RuntimeError(f"Persisted task '{persisted.task_id}' could not be reloaded.")
        self._log_task_outcome(task_snapshot)
        await self._emit_progress(
            submission,
            persisted,
            phase="completed" if task_snapshot.status == TaskStatus.COMPLETED.value else "failed",
            summary=self._task_summary(task_snapshot),
        )
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
                    session_id=latest_run.session_id,
                    worker_type=_enum_value(latest_run.worker_type) or "unknown",
                    workspace_id=latest_run.workspace_id,
                    status=_enum_value(latest_run.status) or WorkerRunStatus.ERROR.value,
                    started_at=latest_run.started_at,
                    finished_at=latest_run.finished_at,
                    summary=latest_run.summary,
                    requested_permission=latest_run.requested_permission,
                    budget_usage=latest_run.budget_usage,
                    verifier_outcome=latest_run.verifier_outcome,
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
        delivery_key: DeliveryKey | None = None,
    ) -> tuple[_PersistedTaskContext | None, str | None]:
        """Create or restore the session scaffolding for a submitted task."""
        now = utc_now()
        with session_scope(self.session_factory) as session:
            delivery_repo = InboundDeliveryRepository(session)
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
            if delivery_key is not None:
                duplicate_task_id = self._link_delivery_to_task(
                    delivery_repo=delivery_repo,
                    delivery_key=delivery_key,
                    task_id=task.id,
                )
                if duplicate_task_id is not None:
                    session.rollback()
                    return None, duplicate_task_id
            return _PersistedTaskContext(
                user_id=user.id,
                session_id=conversation_session.id,
                channel=conversation_session.channel,
                external_thread_id=conversation_session.external_thread_id,
                task_id=task.id,
            ), None

    def _link_delivery_to_task(
        self,
        *,
        delivery_repo: InboundDeliveryRepository,
        delivery_key: DeliveryKey,
        task_id: str,
    ) -> str | None:
        """Atomically bind a delivery key to a task or return the existing duplicate task id."""
        try:
            with delivery_repo.session.begin_nested():
                delivery_repo.create(
                    channel=delivery_key.channel,
                    delivery_id=delivery_key.delivery_id,
                    task_id=task_id,
                )
            return None
        except IntegrityError:
            existing_delivery = delivery_repo.get_by_channel_delivery(
                channel=delivery_key.channel,
                delivery_id=delivery_key.delivery_id,
            )
            if existing_delivery is None:
                raise
            if existing_delivery.task_id is None:
                claimed_delivery = delivery_repo.attach_task_if_unassigned(
                    channel=delivery_key.channel,
                    delivery_id=delivery_key.delivery_id,
                    task_id=task_id,
                )
                if claimed_delivery is not None:
                    return None
                existing_delivery = delivery_repo.get_by_channel_delivery(
                    channel=delivery_key.channel,
                    delivery_id=delivery_key.delivery_id,
                )
                if existing_delivery is None:
                    raise
                if existing_delivery.task_id is None:
                    raise RuntimeError(
                        "Inbound delivery exists without a task_id after dedupe retry."
                    )
            return existing_delivery.task_id

    @staticmethod
    def _task_summary(task_snapshot: TaskSnapshot) -> str | None:
        """Return the latest human-readable outcome summary for notifications."""
        if task_snapshot.latest_run is not None and task_snapshot.latest_run.summary is not None:
            return task_snapshot.latest_run.summary
        return None

    async def _emit_progress(
        self,
        submission: TaskSubmission,
        persisted: _PersistedTaskContext,
        *,
        phase: Literal["started", "running", "completed", "failed"],
        summary: str | None = None,
    ) -> None:
        """Best-effort lifecycle notification that never breaks task execution."""
        if self.progress_notifier is None:
            return
        event = ProgressEvent(
            phase=phase,
            task_id=persisted.task_id,
            session_id=persisted.session_id,
            channel=persisted.channel,
            external_thread_id=persisted.external_thread_id,
            task_text=submission.task_text,
            summary=summary,
        )
        try:
            await self.progress_notifier.notify(submission=submission, event=event)
        except Exception:
            logger.warning(
                "Progress notification failed",
                exc_info=True,
                extra={
                    "session_id": persisted.session_id,
                    "task_id": persisted.task_id,
                    "phase": phase,
                },
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
                session_id=state.session.session_id if state.session is not None else None,
                worker_type=state.dispatch.worker_type,
                workspace_id=_workspace_id_from_artifacts(artifacts),
                started_at=started_at,
                finished_at=finished_at,
                status=_worker_run_status_from_result(state),
                summary=result.summary if result is not None else "Worker did not return a result.",
                requested_permission=result.requested_permission if result is not None else None,
                budget_usage=result.budget_usage if result is not None else None,
                verifier_outcome=_serialize_verification_report(state.verification),
                commands_run=[
                    command.model_dump(mode="json")
                    for command in (result.commands_run if result is not None else [])
                ],
                files_changed_count=len(result.files_changed) if result is not None else 0,
                files_changed=result.files_changed if result is not None else [],
                artifact_index=artifact_index,
            )

            if state.session is not None and state.session_state_update is not None:
                session_state_repo = SessionStateRepository(session)
                session_state_repo.upsert(
                    session_id=state.session.session_id,
                    **state.session_state_update.model_dump(exclude_none=True),
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
