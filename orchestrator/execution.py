"""Execution-path persistence service for the T-044 HTTP vertical slice."""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import logging
import socket
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlparse
from uuid import uuid4

from anyio import to_thread
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from db.base import utc_now
from db.enums import ArtifactType, TaskStatus, WorkerRunStatus, WorkerType
from db.models import (
    Session as ConversationSession,
)
from db.models import (
    Task,
    User,
)
from orchestrator.checkpoints import create_async_sqlite_checkpointer
from orchestrator.graph import build_orchestrator_graph
from orchestrator.state import OrchestratorState, SessionRef
from repositories import (
    ArtifactRepository,
    InboundDeliveryRepository,
    SessionRepository,
    SessionStateRepository,
    TaskRepository,
    TaskTimelineRepository,
    UserRepository,
    WorkerRunRepository,
    session_scope,
)
from workers import ArtifactReference, Worker, WorkerResult

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


def _heartbeat_interval_seconds(*, lease_seconds: int) -> float:
    """Choose a lease heartbeat cadence that scales with lease duration."""
    bounded_lease = max(1, int(lease_seconds))
    return max(1.0, min(10.0, bounded_lease / 3.0))


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
    for family, _, _, _, sockaddr in records:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        if not sockaddr:
            continue
        candidate = sockaddr[0].strip()
        if candidate:
            resolved_addresses.append(candidate)

    if not resolved_addresses:
        raise ValueError("callback_url hostname did not resolve to any addresses.")
    return resolved_addresses


def validate_callback_url(value: str | None) -> str | None:
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


def _validate_callback_url(value: str | None) -> str | None:
    """Backward-compatible alias for callers/tests that still reference the private name."""
    return validate_callback_url(value)


class TaskSubmission(ExecutionModel):
    """HTTP payload accepted by the minimal task-submission endpoint."""

    task_text: str = Field(min_length=1)
    repo_url: str | None = None
    branch: str | None = None
    priority: int = Field(default=0, ge=0)
    worker_override: WorkerType | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    tools: list[str] | None = None
    callback_url: str | None = Field(default=None, max_length=2048)
    session: SubmissionSession = Field(default_factory=SubmissionSession)

    @field_validator("callback_url")
    @classmethod
    def validate_callback_url(cls, value: str | None) -> str | None:
        """Ensure callback URLs are safe for outbound progress delivery."""
        return validate_callback_url(value)


class TaskApprovalDecision(ExecutionModel):
    """Decision payload for a paused task approval checkpoint."""

    approved: bool


class TaskReplayRequest(ExecutionModel):
    """Optional overrides when replaying an existing task."""

    worker_override: WorkerType | None = None
    constraints: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    secrets: dict[str, str] | None = None


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


class TaskTimelineEventSnapshot(ExecutionModel):
    """A granular event in a task's lifecycle (T-090)."""

    event_type: str
    attempt_number: int = 0
    sequence_number: int = 0
    message: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime


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
    timeline: list[TaskTimelineEventSnapshot] = Field(default_factory=list)


class OperationalMetrics(ExecutionModel):
    """Aggregated operational metrics for the service (T-092)."""

    total_tasks: int
    retried_tasks: int
    retry_rate: float
    status_counts: dict[str, int]
    worker_usage: dict[str, int]
    avg_duration_seconds: float
    success_rate: float


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
    attempt_count: int


@dataclass(frozen=True)
class CreateTaskOutcome:
    """Result of persisting or deduping a submitted task."""

    task_snapshot: TaskSnapshot
    persisted: _PersistedTaskContext | None
    duplicate: bool = False


@dataclass(frozen=True)
class TaskClaim:
    """A claimed task ready for worker execution."""

    task_id: str
    attempt_count: int
    max_attempts: int


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


@dataclass(frozen=True)
class ApprovalDecisionResult:
    """Outcome of applying an approval decision to a paused task."""

    status: Literal["applied", "already_applied", "not_waiting", "conflict", "not_found"]
    task_snapshot: TaskSnapshot | None = None
    detail: str | None = None


_REPLAYABLE_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
)


@dataclass(frozen=True)
class TaskReplayResult:
    """Outcome of replaying a prior task."""

    status: Literal["created", "not_found", "not_replayable"]
    task_snapshot: TaskSnapshot | None = None
    source_task_id: str | None = None
    detail: str | None = None


def _deep_merge(
    target: dict[str, Any],
    source: dict[str, Any],
    *,
    reserved_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Recursively merge two dictionaries, returning a new result."""
    merged = copy.deepcopy(target)

    def strip_reserved(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: strip_reserved(v)
                for k, v in obj.items()
                if not (reserved_keys and k in reserved_keys)
            }
        if isinstance(obj, list):
            return [strip_reserved(v) for v in obj]
        return copy.deepcopy(obj)

    def merge_in_place(base: dict[str, Any], overrides: dict[str, Any]) -> None:
        for key, value in overrides.items():
            if reserved_keys and key in reserved_keys:
                continue
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                merge_in_place(base[key], value)
            else:
                base[key] = strip_reserved(value)

    merge_in_place(merged, source)
    return merged


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


def _worker_type_for_persistence(state: OrchestratorState) -> WorkerType:
    """Choose a persisted worker type even when dispatch metadata is incomplete."""
    if state.dispatch.worker_type is not None:
        return WorkerType(state.dispatch.worker_type)

    if state.route.chosen_worker is not None:
        logger.warning(
            "Persisting worker run with route fallback because dispatch worker type is missing.",
            extra={"route_worker": state.route.chosen_worker},
        )
        return WorkerType(state.route.chosen_worker)

    logger.warning(
        "Persisting worker run with codex default because worker type is missing.",
    )
    return WorkerType.CODEX


def _interrupt_payload_from_object(interrupt: object) -> dict[str, Any] | None:
    """Extract an interrupt payload mapping from LangGraph interrupt objects."""
    if isinstance(interrupt, Mapping):
        candidate = interrupt.get("value")
        if isinstance(candidate, Mapping):
            return dict(candidate)
        return dict(interrupt)

    candidate = getattr(interrupt, "value", None)
    if isinstance(candidate, Mapping):
        return dict(candidate)
    return None


def _interrupt_summary(payloads: list[dict[str, Any]]) -> str:
    """Build a concise failure summary when orchestration stops on an interrupt."""
    first = payloads[0] if payloads else {}
    approval_type = str(first.get("approval_type") or "").strip()
    reason = str(first.get("reason") or "").strip()
    requested_permission = str(first.get("requested_permission") or "").strip()

    if approval_type == "permission_escalation":
        if requested_permission:
            summary = (
                f"Run paused pending permission escalation approval for '{requested_permission}'."
            )
        else:
            summary = "Run paused pending permission escalation approval."
    elif approval_type:
        display_approval_type = approval_type.replace("_", " ")
        suffix = "" if display_approval_type.endswith("approval") else " approval"
        summary = f"Run paused pending {display_approval_type}{suffix}."
    else:
        summary = "Run paused pending manual approval."

    if reason:
        summary = f"{summary} {reason}"
    return summary


def _normalize_orchestrator_graph_output(raw_output: object) -> object:
    """Strip transport-only interrupt keys and map unresolved interrupts to failure output."""
    if not isinstance(raw_output, Mapping):
        return raw_output

    normalized = dict(raw_output)
    interrupts_raw = normalized.pop("__interrupt__", None)
    if interrupts_raw is None:
        return normalized

    interrupts: list[object]
    if isinstance(interrupts_raw, list):
        interrupts = list(interrupts_raw)
    else:
        interrupts = [interrupts_raw]

    payloads = [
        payload
        for payload in (_interrupt_payload_from_object(interrupt) for interrupt in interrupts)
        if payload is not None
    ]
    logger.warning(
        "Orchestrator graph returned unresolved interrupts; normalizing result for persistence.",
        extra={"interrupt_count": len(payloads) or len(interrupts)},
    )

    existing_errors = normalized.get("errors")
    errors: list[str]
    if isinstance(existing_errors, list):
        errors = [str(item) for item in existing_errors]
    else:
        errors = []
    errors.append("orchestrator interrupted awaiting manual approval")
    normalized["errors"] = errors

    existing_progress = normalized.get("progress_updates")
    progress_updates: list[str]
    if isinstance(existing_progress, list):
        progress_updates = [str(item) for item in existing_progress]
    else:
        progress_updates = []
    progress_updates.append("run interrupted pending manual approval")
    normalized["progress_updates"] = progress_updates

    if normalized.get("result") is None:
        first_payload = payloads[0] if payloads else {}
        requested_permission = first_payload.get("requested_permission")
        normalized["result"] = WorkerResult(
            status="failure",
            summary=_interrupt_summary(payloads),
            requested_permission=(
                str(requested_permission).strip() if requested_permission is not None else None
            ),
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="await_manual_follow_up",
        ).model_dump(mode="json")
    return normalized


def _requires_manual_follow_up(state: OrchestratorState) -> bool:
    """Return True when a failed result should remain terminal for operator action."""
    if state.result is None:
        return False
    return state.result.next_action_hint == "await_manual_follow_up"


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


def _approval_constraints_payload(
    *,
    status: str,
    approval_type: str | None,
    reason: str | None,
    resume_token: str | None,
    updated_at: datetime,
    source: str,
    approved: bool | None = None,
) -> dict[str, Any]:
    """Build the persisted approval checkpoint payload stored in task constraints."""
    payload: dict[str, Any] = {
        "status": status,
        "approval_type": approval_type,
        "reason": reason,
        "resume_token": resume_token,
        "updated_at": updated_at.isoformat(),
        "source": source,
    }
    if approved is not None:
        payload["approved"] = approved
    return payload


class TaskExecutionService:
    """Submit tasks through the orchestrator and persist execution-path state."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        worker: Worker,
        gemini_worker: Worker | None = None,
        progress_notifier: ProgressNotifier | None = None,
        default_task_max_attempts: int = 3,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.worker = worker
        self.gemini_worker = gemini_worker
        self.progress_notifier = progress_notifier
        self.default_task_max_attempts = max(1, int(default_task_max_attempts))
        self.checkpoint_path = checkpoint_path
        self._checkpointer: BaseCheckpointSaver | None = None
        self._checkpointer_cm: AbstractAsyncContextManager[BaseCheckpointSaver] | None = None
        self._graph: Any | None = None

    @property
    def graph(self) -> Any:
        """Lazy-loaded orchestrator graph, compiled with the current checkpointer."""
        if self._graph is None:
            self._graph = build_orchestrator_graph(
                worker=self.worker,
                gemini_worker=self.gemini_worker,
                checkpointer=self._checkpointer,
            )
        return self._graph

    async def __aenter__(self) -> TaskExecutionService:
        """Initialize shared resources (like checkpointers) if configured."""
        if self.checkpoint_path and not self._checkpointer:
            self._checkpointer_cm = create_async_sqlite_checkpointer(self.checkpoint_path)
            self._checkpointer = await self._checkpointer_cm.__aenter__()
            # Invalidate graph to force recompile with checkpointer
            self._graph = None
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Close shared resources."""
        if self._checkpointer_cm:
            await self._checkpointer_cm.__aexit__(exc_type, exc_val, exc_tb)
            self._checkpointer = None
            self._checkpointer_cm = None
            # Invalidate graph to revert to memory-only
            self._graph = None

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
            max_attempts=self.default_task_max_attempts,
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
        """Legacy direct execution entrypoint kept for compatibility/tests."""
        await self._run_blocking(self._mark_task_in_progress, task_id=persisted.task_id)
        await self._emit_progress(submission, persisted, phase="started")
        await self._emit_progress(submission, persisted, phase="running")
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

    async def run_queued_task(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> None:
        """Execute one claimed queued task id and persist/release queue state."""
        loaded = await self._run_blocking(self._load_submission_for_task, task_id=task_id)
        if loaded is None:
            logger.warning(
                "Skipping queued task run: task no longer exists",
                extra={"task_id": task_id},
            )
            return None

        submission, persisted = loaded
        await self._emit_progress(submission, persisted, phase="started")
        await self._emit_progress(submission, persisted, phase="running")

        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                task_id=task_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            ),
            name=f"task-heartbeat-{task_id}",
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
                "worker_id": worker_id,
            },
        )
        try:
            state = await self._run_orchestrator(submission, persisted)
            finished_at = utc_now()
            if state.result is not None and state.result.status == "success":
                await self._run_blocking(
                    self._persist_execution_outcome,
                    task_id=persisted.task_id,
                    state=state,
                    started_at=started_at,
                    finished_at=finished_at,
                    force_task_status=TaskStatus.COMPLETED,
                )
                await self._run_blocking(self._release_task_success, task_id=persisted.task_id)
            else:
                terminal_failure = _requires_manual_follow_up(state)
                await self._run_blocking(
                    self._persist_execution_outcome,
                    task_id=persisted.task_id,
                    state=state,
                    started_at=started_at,
                    finished_at=finished_at,
                    force_task_status=(
                        TaskStatus.FAILED if terminal_failure else TaskStatus.IN_PROGRESS
                    ),
                )
                if terminal_failure:
                    await self._run_blocking(
                        self._release_task_terminal_failure,
                        task_id=persisted.task_id,
                        worker_id=worker_id,
                    )
                else:
                    await self._run_blocking(
                        self._release_task_failure,
                        task_id=persisted.task_id,
                        worker_id=worker_id,
                    )
        except Exception as exc:
            logger.exception(
                "Task execution failed before the final outcome was fully persisted",
                extra={
                    "session_id": persisted.session_id,
                    "task_id": persisted.task_id,
                    "worker_id": worker_id,
                },
            )
            await self._run_blocking(
                self._record_task_attempt_error,
                task_id=persisted.task_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            await self._run_blocking(
                self._release_task_failure,
                task_id=persisted.task_id,
                worker_id=worker_id,
            )
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

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

    async def _heartbeat_loop(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> None:
        """Best-effort lease heartbeat while task execution is in progress."""
        sleep_seconds = _heartbeat_interval_seconds(lease_seconds=lease_seconds)
        while True:
            await asyncio.sleep(sleep_seconds)
            ok = await self._run_blocking(
                self._heartbeat_task_lease,
                task_id=task_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
            if not ok:
                return None

    def claim_next_task(self, *, worker_id: str, lease_seconds: int) -> TaskClaim | None:
        """Claim one queued task for worker execution."""
        with session_scope(self.session_factory) as session:
            task_repo = TaskRepository(session)
            task = task_repo.claim_next(
                worker_id=worker_id,
                now=utc_now(),
                lease_seconds=lease_seconds,
            )
            if task is None:
                return None
            return TaskClaim(
                task_id=task.id,
                attempt_count=task.attempt_count,
                max_attempts=task.max_attempts,
            )

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
                timeline=[
                    TaskTimelineEventSnapshot(
                        event_type=_enum_value(event.event_type) or "unknown",
                        attempt_number=event.attempt_number,
                        sequence_number=event.sequence_number,
                        message=event.message,
                        payload=event.payload,
                        created_at=event.created_at,
                    )
                    for event in task.timeline_events
                ],
            )

    def apply_task_approval_decision(
        self,
        *,
        task_id: str,
        approved: bool,
    ) -> ApprovalDecisionResult:
        """Apply an idempotent approval decision for a paused task."""
        decided_at = utc_now()
        with session_scope(self.session_factory) as session:
            task_repo = TaskRepository(session)
            worker_run_repo = WorkerRunRepository(session)

            task = task_repo.get(task_id)
            if task is None:
                return ApprovalDecisionResult(
                    status="not_found",
                    detail=f"Task '{task_id}' was not found.",
                )

            constraints = dict(task.constraints or {})
            approval_state_raw = constraints.get("approval")
            approval_state = (
                dict(approval_state_raw) if isinstance(approval_state_raw, Mapping) else None
            )
            if approval_state is None:
                return ApprovalDecisionResult(
                    status="not_waiting",
                    detail="Task is not currently awaiting a manual approval decision.",
                )

            current_status = str(approval_state.get("status") or "").strip().lower()
            requested_status = "approved" if approved else "rejected"
            if current_status in {"approved", "rejected"}:
                if current_status == requested_status:
                    return ApprovalDecisionResult(
                        status="already_applied",
                        task_snapshot=self.get_task(task_id),
                    )
                return ApprovalDecisionResult(
                    status="conflict",
                    detail=(
                        "Task approval decision already recorded as "
                        f"'{current_status}' and cannot be changed."
                    ),
                )
            if current_status != "pending":
                return ApprovalDecisionResult(
                    status="not_waiting",
                    detail="Task is not currently awaiting a manual approval decision.",
                )

            approval_type = str(approval_state.get("approval_type") or "").strip() or None
            reason = str(approval_state.get("reason") or "").strip() or None
            resume_token = str(approval_state.get("resume_token") or "").strip() or None
            constraints["approval"] = _approval_constraints_payload(
                status=requested_status,
                approval_type=approval_type,
                reason=reason,
                resume_token=resume_token,
                updated_at=decided_at,
                source="api",
                approved=approved,
            )

            if approved:
                constraints["requires_approval"] = False
                task.status = TaskStatus.PENDING
                task.next_attempt_at = decided_at
                task.last_error = None
            else:
                task.status = TaskStatus.FAILED
                task.next_attempt_at = None
                task.last_error = "Manual approval rejected via API decision endpoint."
                worker_type = task.chosen_worker or task.worker_override or WorkerType.CODEX
                worker_run_repo.create(
                    task_id=task.id,
                    session_id=task.session_id,
                    worker_type=worker_type,
                    workspace_id=None,
                    started_at=decided_at,
                    finished_at=decided_at,
                    status=WorkerRunStatus.FAILURE,
                    summary=(
                        "Manual approval rejected via API decision endpoint; task remains failed."
                    ),
                    commands_run=[],
                    files_changed_count=0,
                    files_changed=[],
                    artifact_index=[],
                )

            task.constraints = constraints
            task.lease_owner = None
            task.lease_expires_at = None
            session.flush()

        snapshot = self.get_task(task_id)
        if snapshot is None:
            return ApprovalDecisionResult(
                status="not_found",
                detail=f"Task '{task_id}' was not found after applying decision.",
            )
        return ApprovalDecisionResult(status="applied", task_snapshot=snapshot)

    def get_operational_metrics(self, window_hours: int | None = 24) -> OperationalMetrics:
        """Return aggregated operational metrics across tasks and runs."""
        since = None
        if window_hours:
            since = utc_now() - timedelta(hours=window_hours)

        with session_scope(self.session_factory) as session:
            task_repo = TaskRepository(session)
            run_repo = WorkerRunRepository(session)

            task_metrics = task_repo.get_metrics(since=since)
            run_metrics = run_repo.get_metrics(since=since)

            return OperationalMetrics(
                total_tasks=task_metrics["total_tasks"],
                retried_tasks=task_metrics["retried_tasks"],
                retry_rate=task_metrics["retry_rate"],
                status_counts=task_metrics["status_counts"],
                worker_usage=run_metrics["worker_usage"],
                avg_duration_seconds=run_metrics["avg_duration_seconds"],
                success_rate=run_metrics["success_rate"],
            )

    def is_secret_encryption_active(self) -> bool:
        """Return True if secret encryption is active."""
        # We check the model directly to see if the decorator is active.
        return Task.is_secret_encryption_active()

    def replay_task(
        self,
        *,
        source_task_id: str,
        replay_request: TaskReplayRequest | None = None,
    ) -> TaskReplayResult:
        """Create a new task by replaying a prior terminal task with optional overrides."""
        source_snapshot = self.get_task(source_task_id)
        if source_snapshot is None:
            return TaskReplayResult(
                status="not_found",
                source_task_id=source_task_id,
                detail=f"Task '{source_task_id}' was not found.",
            )
        if source_snapshot.status not in _REPLAYABLE_STATUSES:
            return TaskReplayResult(
                status="not_replayable",
                source_task_id=source_task_id,
                detail=(
                    f"Task '{source_task_id}' has status '{source_snapshot.status}' "
                    f"and cannot be replayed. Only terminal tasks "
                    f"(completed, failed, cancelled) are replayable."
                ),
            )

        loaded = self._load_submission_for_task(task_id=source_task_id)
        if loaded is None:
            return TaskReplayResult(
                status="not_found",
                source_task_id=source_task_id,
                detail=(
                    f"Task '{source_task_id}' exists but its session or user "
                    f"could not be resolved for replay."
                ),
            )
        submission, _ = loaded

        # Apply caller overrides and tag provenance with an audit chain
        updates: dict[str, Any] = {}
        if replay_request is not None:
            if replay_request.worker_override is not None:
                updates["worker_override"] = replay_request.worker_override
            if replay_request.constraints is not None:
                updates["constraints"] = _deep_merge(
                    submission.constraints,
                    replay_request.constraints,
                    reserved_keys={"replayed_from"},
                )
            if replay_request.budget is not None:
                updates["budget"] = _deep_merge(
                    submission.budget,
                    replay_request.budget,
                    reserved_keys={"replayed_from"},
                )
            if replay_request.secrets is not None:
                updates["secrets"] = dict(replay_request.secrets)

        # Ensure provenance chain is included in the final set of constraints
        base_constraints = updates.get("constraints", submission.constraints)
        existing_chain_raw = base_constraints.get("replayed_from")
        existing_chain: list[str]

        if isinstance(existing_chain_raw, str):
            # Migration safety for tasks created before replayed_from was a list
            existing_chain = [existing_chain_raw]
        elif isinstance(existing_chain_raw, list):
            existing_chain = list(existing_chain_raw)
        elif existing_chain_raw is None:
            existing_chain = []
        else:
            logger.warning(
                "Unexpected replayed_from type in task constraints; resetting provenance chain.",
                extra={
                    "task_id": source_task_id,
                    "actual_type": type(existing_chain_raw).__name__,
                },
            )
            existing_chain = []

        # Filter out the current source_task_id if it already exists in the chain
        # to prevent redundant entries in the audit trail.
        existing_chain = [tid for tid in existing_chain if tid != source_task_id]

        if "constraints" not in updates:
            updates["constraints"] = copy.deepcopy(base_constraints)

        updates["constraints"]["replayed_from"] = [source_task_id, *existing_chain]

        submission = submission.model_copy(update=updates)

        task_snapshot, _ = self.create_task(submission)
        logger.info(
            "Replayed task created from source",
            extra={
                "source_task_id": source_task_id,
                "new_task_id": task_snapshot.task_id,
                "worker_override": submission.worker_override,
            },
        )
        return TaskReplayResult(
            status="created",
            task_snapshot=task_snapshot,
            source_task_id=source_task_id,
        )

    def _persist_submission(
        self,
        submission: TaskSubmission,
        *,
        status: TaskStatus,
        max_attempts: int,
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
                callback_url=submission.callback_url,
                worker_override=submission.worker_override,
                budget=dict(submission.budget),
                secrets=dict(submission.secrets),
                # Store tools in constraints to avoid a schema migration for now
                constraints={
                    **(submission.constraints or {}),
                    "tools": submission.tools,
                }
                if submission.tools is not None
                else dict(submission.constraints),
                secrets_encrypted=self.is_secret_encryption_active(),
                status=status,
                max_attempts=max(1, max_attempts),
                next_attempt_at=now,
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
                attempt_count=task.attempt_count,
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

        def _get_count() -> int:
            with session_scope(self.session_factory) as session:
                return TaskTimelineRepository(session).count_by_attempt(
                    task_id=persisted.task_id, attempt_number=persisted.attempt_count
                )

        initial_persisted_count = await self._run_blocking(_get_count)

        config = {"configurable": {"thread_id": persisted.task_id}}

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
                    "secrets": dict(submission.secrets),
                    "tools": submission.tools,
                },
                "attempt_count": persisted.attempt_count,
                "timeline_persisted_count": initial_persisted_count,
            },
            config=config,
        )
        normalized_output = _normalize_orchestrator_graph_output(raw_output)
        return OrchestratorState.model_validate(normalized_output)

    def _load_submission_for_task(
        self,
        *,
        task_id: str,
    ) -> tuple[TaskSubmission, _PersistedTaskContext] | None:
        """Reconstruct a worker-run submission payload from persisted task/session state."""
        with session_scope(self.session_factory) as session:
            task_repo = TaskRepository(session)
            session_repo = SessionRepository(session)
            user_repo = UserRepository(session)

            task = task_repo.get(task_id)
            if task is None:
                return None
            conversation_session = session_repo.get(task.session_id)
            if conversation_session is None:
                return None
            user = user_repo.get(conversation_session.user_id)
            if user is None:
                return None
            submission = TaskSubmission(
                task_text=task.task_text,
                repo_url=task.repo_url,
                branch=task.branch,
                worker_override=task.worker_override,
                constraints=dict(task.constraints or {}),
                budget=dict(task.budget or {}),
                secrets=dict(task.secrets or {}),
                callback_url=task.callback_url,
                tools=(task.constraints or {}).get("tools")
                if isinstance(task.constraints, dict)
                else None,
                priority=task.priority,
                session=SubmissionSession(
                    channel=conversation_session.channel,
                    external_user_id=user.external_user_id or "unknown",
                    external_thread_id=conversation_session.external_thread_id,
                    display_name=user.display_name,
                ),
            )
            persisted = _PersistedTaskContext(
                user_id=user.id,
                session_id=conversation_session.id,
                channel=conversation_session.channel,
                external_thread_id=conversation_session.external_thread_id,
                task_id=task.id,
                attempt_count=task.attempt_count,
            )
            return submission, persisted

    def _mark_task_in_progress(self, *, task_id: str) -> None:
        """Mark a queued task as in progress when direct execution begins."""
        with session_scope(self.session_factory) as session:
            TaskRepository(session).update_status(task_id=task_id, status=TaskStatus.IN_PROGRESS)

    def _mark_task_failed(self, *, task_id: str) -> None:
        """Mark a task as failed in legacy direct execution path."""
        with session_scope(self.session_factory) as session:
            TaskRepository(session).update_status(task_id=task_id, status=TaskStatus.FAILED)

    def _release_task_success(self, *, task_id: str) -> None:
        """Mark a task attempt successful in queue state."""
        with session_scope(self.session_factory) as session:
            TaskRepository(session).release_success(task_id=task_id)

    def _release_task_failure(self, *, task_id: str, worker_id: str) -> None:
        """Release a failed task attempt, requeueing when attempts remain."""
        with session_scope(self.session_factory) as session:
            TaskRepository(session).release_failure(
                task_id=task_id,
                worker_id=worker_id,
                now=utc_now(),
                retry_backoff_seconds=15,
            )

    def _release_task_terminal_failure(self, *, task_id: str, worker_id: str) -> None:
        """Release queue lease while preserving terminal failure for manual follow-up."""
        with session_scope(self.session_factory) as session:
            TaskRepository(session).release_terminal_failure(
                task_id=task_id,
                worker_id=worker_id,
            )

    def _record_task_attempt_error(self, *, task_id: str, error: str) -> None:
        """Persist an execution exception snippet on the task row."""
        with session_scope(self.session_factory) as session:
            TaskRepository(session).record_attempt_error(task_id=task_id, error_text=error)

    def _heartbeat_task_lease(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        """Extend queue lease for one in-flight task, returning False when lease is lost."""
        with session_scope(self.session_factory) as session:
            return TaskRepository(session).heartbeat_lease(
                task_id=task_id,
                worker_id=worker_id,
                now=utc_now(),
                lease_seconds=lease_seconds,
            )

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
        force_task_status: TaskStatus | None = None,
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

            task_repo.update_status(
                task_id=task_id,
                status=force_task_status or _task_status_from_result(state),
            )
            task = task_repo.get(task_id)
            if task is None:
                raise RuntimeError(f"Task '{task_id}' disappeared while persisting execution.")

            approval = state.approval
            if approval.required:
                approval_status = (
                    "pending"
                    if _requires_manual_follow_up(state) and approval.status == "pending"
                    else approval.status
                )
                if approval_status in {"pending", "approved", "rejected"}:
                    constraints = dict(task.constraints or {})
                    constraints["approval"] = _approval_constraints_payload(
                        status=approval_status,
                        approval_type=approval.approval_type,
                        reason=approval.reason,
                        resume_token=approval.resume_token,
                        updated_at=finished_at,
                        source="orchestrator",
                        approved=(
                            True
                            if approval_status == "approved"
                            else False
                            if approval_status == "rejected"
                            else None
                        ),
                    )
                    task.constraints = constraints

            result = state.result
            artifacts = result.artifacts if result is not None else []
            artifact_index = [artifact.model_dump(mode="json") for artifact in artifacts]
            worker_type = _worker_type_for_persistence(state)
            worker_run = worker_run_repo.create(
                task_id=task_id,
                session_id=state.session.session_id if state.session is not None else None,
                worker_type=worker_type,
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

            # Use the state-side marker of already-persisted events to avoid redundant DB queries
            # This marker is initialized from the DB at the start of the task execution attempt.
            persisted_count = state.timeline_persisted_count

            current_attempt_events = []
            for e in reversed(state.timeline_events):
                if e.attempt_number != state.attempt_count:
                    break
                current_attempt_events.append(e)
            current_attempt_events.reverse()

            # Filter for events that have not been persisted yet
            # Since sequence_number is 0-indexed, skip already persisted events
            new_events = [e for e in current_attempt_events if e.sequence_number >= persisted_count]

            if new_events:
                timeline_repo = TaskTimelineRepository(session)
                timeline_repo.create_batch(
                    task_id=task_id,
                    events=[e.model_dump() for e in new_events],
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


class TaskQueueWorker:
    """Long-running queue poller that claims and executes queued tasks."""

    def __init__(
        self,
        *,
        service: TaskExecutionService,
        worker_id: str | None = None,
        poll_interval_seconds: float = 2.0,
        lease_seconds: int = 60,
    ) -> None:
        self.service = service
        self.worker_id = worker_id or f"worker-{uuid4().hex[:8]}"
        self.poll_interval_seconds = max(0.25, float(poll_interval_seconds))
        self.lease_seconds = max(15, int(lease_seconds))

    async def run_forever(self) -> None:
        """Poll for queued tasks indefinitely."""
        logger.info(
            "Starting task queue worker loop",
            extra={
                "worker_id": self.worker_id,
                "poll_interval_seconds": self.poll_interval_seconds,
                "lease_seconds": self.lease_seconds,
            },
        )
        async with self.service:
            while True:
                claim = await self.service._run_blocking(
                    self.service.claim_next_task,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
                if claim is None:
                    await asyncio.sleep(self.poll_interval_seconds)
                    continue
                await self.service.run_queued_task(
                    task_id=claim.task_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
