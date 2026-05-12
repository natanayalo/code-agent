"""Execution-path persistence service for the T-044 HTTP vertical slice."""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import logging
import os
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import unquote, urlparse
from uuid import uuid4

from anyio import to_thread
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from apps.observability import (
    ATTR_WORKER_ID,
    SPAN_KIND_AGENT,
    bind_current_trace_context,
    capture_trace_context,
    is_tracing_enabled,
    record_span_exception,
    resolve_otel_tracing_endpoint,
    resolve_tracing_project_name,
    set_current_span_attribute,
    set_span_input_output,
    set_span_status_from_outcome,
    start_optional_span,
    with_restored_trace_context,
    with_span_kind,
)
from db.base import utc_now
from db.enums import (
    ArtifactType,
    HumanInteractionStatus,
    HumanInteractionType,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import HumanInteraction, PersonalMemory, ProjectMemory, Task, User, WorkerRun
from db.models import (
    Session as ConversationSession,
)
from orchestrator.brain import OrchestratorBrain
from orchestrator.checkpoints import create_async_sqlite_checkpointer
from orchestrator.graph import build_orchestrator_graph
from orchestrator.state import (
    OrchestratorState,
    SessionRef,
    TaskSpec,
    compute_interaction_content_hash,
)
from orchestrator.task_spec import build_task_spec
from repositories import (
    ArtifactRepository,
    HumanInteractionRepository,
    InboundDeliveryRepository,
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    SessionRepository,
    SessionStateRepository,
    TaskRepository,
    TaskTimelineRepository,
    UserRepository,
    WorkerRunRepository,
    session_scope,
)
from tools import coerce_permission_level
from tools.numeric import coerce_non_negative_int_like, coerce_positive_int_like
from workers import ArtifactReference, Worker, WorkerProfile, WorkerResult

logger = logging.getLogger(__name__)

_CALLBACK_RESOLUTION_TIMEOUT_SECONDS = 2.0
_CALLBACK_DNS_EXECUTOR_MAX_WORKERS = 4
_callback_dns_executor: ThreadPoolExecutor | None = None
_callback_dns_executor_lock = Lock()
_INTERACTIVE_EXECUTION_MODE = "interactive"
_UNATTENDED_EXECUTION_MODE = "unattended"
_VALID_EXECUTION_MODES = frozenset({_INTERACTIVE_EXECUTION_MODE, _UNATTENDED_EXECUTION_MODE})


# T-102: global defaults and hard caps for runtime budgets.
_DEFAULT_EXECUTION_BUDGETS: dict[str, dict[str, int]] = {
    _INTERACTIVE_EXECUTION_MODE: {
        "max_iterations": 8,
        "worker_timeout_seconds": 600,
        "max_tool_calls": 24,
        "max_shell_commands": 24,
        "max_retries": 2,
    },
    _UNATTENDED_EXECUTION_MODE: {
        "max_iterations": 5,
        "worker_timeout_seconds": 300,
        "max_tool_calls": 12,
        "max_shell_commands": 12,
        "max_retries": 1,
    },
}
_GLOBAL_BUDGET_CAPS: dict[str, int] = {
    "max_iterations": 20,
    "worker_timeout_seconds": 900,
    "max_minutes": 15,
    "orchestrator_timeout_seconds": 930,
    "command_timeout_seconds": 300,
    "max_tool_calls": 100,
    "max_shell_commands": 100,
    "max_retries": 10,
    "max_verifier_passes": 5,
    "max_observation_characters": 12000,
}
_NON_NEGATIVE_BUDGET_KEYS = frozenset(
    {"max_retries", "max_verifier_passes", "max_tool_calls", "max_shell_commands"}
)
_NON_NEGATIVE_DEFAULT_BUDGET_KEYS = frozenset(
    {"max_retries", "max_tool_calls", "max_shell_commands"}
)


class ExecutionModel(BaseModel):
    """Base model for task-execution service payloads."""

    model_config = ConfigDict(extra="forbid")


class InteractionResponse(ExecutionModel):
    """Payload for submitting a response to a human interaction."""

    response_data: dict[str, Any]
    status: HumanInteractionStatus = HumanInteractionStatus.RESOLVED


class SubmissionSession(ExecutionModel):
    """Caller identity and thread metadata for a submitted task."""

    channel: str = Field(default="http", min_length=1)
    external_user_id: str = Field(default="http:anonymous", min_length=1)
    external_thread_id: str = Field(default="http-default", min_length=1)
    display_name: str | None = None


def _resolve_execution_mode(
    *,
    channel: str,
    constraints: Mapping[str, Any],
    budget: Mapping[str, Any],
) -> str:
    """Resolve execution mode with explicit overrides before channel defaults."""
    # Constraints are policy/operator inputs and should win over user budget hints.
    candidates = (constraints.get("execution_mode"), budget.get("execution_mode"))
    for candidate in candidates:
        if isinstance(candidate, str):
            normalized = candidate.strip().lower()
            if normalized in _VALID_EXECUTION_MODES:
                return normalized
    normalized_channel = channel.strip().lower()
    return (
        _INTERACTIVE_EXECUTION_MODE
        if normalized_channel == "telegram"
        else _UNATTENDED_EXECUTION_MODE
    )


def _apply_execution_budget_policy(
    *,
    channel: str,
    constraints: Mapping[str, Any],
    budget: Mapping[str, Any],
) -> dict[str, Any]:
    """Return an effective runtime budget with mode defaults and global hard caps."""
    execution_mode = _resolve_execution_mode(
        channel=channel, constraints=constraints, budget=budget
    )
    effective_budget: dict[str, Any] = dict(budget)
    effective_budget["execution_mode"] = execution_mode

    for key, default_value in _DEFAULT_EXECUTION_BUDGETS[execution_mode].items():
        # Preserve max_minutes as an alternate timeout input when worker_timeout_seconds
        # is not explicitly set by the caller.
        if (
            key == "worker_timeout_seconds"
            and coerce_positive_int_like(effective_budget.get("worker_timeout_seconds")) is None
            and coerce_positive_int_like(effective_budget.get("max_minutes")) is not None
        ):
            continue
        coercer = (
            coerce_non_negative_int_like
            if key in _NON_NEGATIVE_DEFAULT_BUDGET_KEYS
            else coerce_positive_int_like
        )
        if coercer(effective_budget.get(key)) is None:
            effective_budget[key] = default_value

    for key, cap in _GLOBAL_BUDGET_CAPS.items():
        coercer = (
            coerce_non_negative_int_like
            if key in _NON_NEGATIVE_BUDGET_KEYS
            else coerce_positive_int_like
        )
        coerced_value = coercer(effective_budget.get(key))
        if coerced_value is not None:
            effective_budget[key] = min(coerced_value, cap)
        elif key in effective_budget:
            effective_budget.pop(key, None)

    return effective_budget


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
    worker_profile_override: str | None = Field(default=None, min_length=1, max_length=255)
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
    worker_profile_override: str | None = Field(default=None, min_length=1, max_length=255)
    constraints: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    secrets: dict[str, str] | None = None


class TaskSubmissionValidationError(ValueError):
    """Raised when a task submission payload is semantically invalid."""


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
    worker_profile: str | None = None
    runtime_mode: str | None = None
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
    files_changed: list[str] = Field(default_factory=list)
    artifact_index: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[ArtifactSnapshot] = Field(default_factory=list)


class TaskTimelineEventSnapshot(ExecutionModel):
    """A granular event in a task's lifecycle (T-090)."""

    id: str
    event_type: str
    attempt_number: int = 0
    sequence_number: int = 0
    message: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime


class SessionSnapshot(ExecutionModel):
    """The persisted session view returned by session listing/detail endpoints."""

    session_id: str
    user_id: str
    channel: str
    external_thread_id: str
    active_task_id: str | None = None
    status: str
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    working_context: SessionWorkingContextSnapshot | None = None


class SessionWorkingContextSnapshot(ExecutionModel):
    """Compact working context persisted for a session."""

    active_goal: str | None = None
    decisions_made: dict[str, Any] = Field(default_factory=dict)
    identified_risks: dict[str, Any] = Field(default_factory=dict)
    files_touched: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class PersonalMemorySnapshot(ExecutionModel):
    """A persisted user-scoped skeptical memory entry."""

    memory_id: str
    user_id: str
    memory_key: str
    value: dict[str, Any]
    source: str | None = None
    confidence: float = 1.0
    scope: str | None = None
    last_verified_at: datetime | None = None
    requires_verification: bool = True
    created_at: datetime
    updated_at: datetime


class ProjectMemorySnapshot(ExecutionModel):
    """A persisted repository-scoped skeptical memory entry."""

    memory_id: str
    repo_url: str
    memory_key: str
    value: dict[str, Any]
    source: str | None = None
    confidence: float = 1.0
    scope: str | None = None
    last_verified_at: datetime | None = None
    requires_verification: bool = True
    created_at: datetime
    updated_at: datetime


class PersonalMemoryUpsertRequest(ExecutionModel):
    """Input payload for creating/updating a personal memory entry."""

    user_id: str = Field(min_length=1)
    memory_key: str = Field(min_length=1)
    value: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    scope: str | None = None
    last_verified_at: datetime | None = None
    requires_verification: bool = True


class ProjectMemoryUpsertRequest(ExecutionModel):
    """Input payload for creating/updating a project memory entry."""

    repo_url: str = Field(min_length=1)
    memory_key: str = Field(min_length=1)
    value: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    scope: str | None = None
    last_verified_at: datetime | None = None
    requires_verification: bool = True


class TaskSummarySnapshot(ExecutionModel):
    """A lightweight task view for listing endpoints (T-131)."""

    task_id: str
    session_id: str
    status: str
    task_text: str
    repo_url: str | None = None
    branch: str | None = None
    priority: int = 0
    chosen_worker: str | None = None
    chosen_profile: str | None = None
    runtime_mode: str | None = None
    route_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    latest_run_id: str | None = None
    latest_run_status: str | None = None
    latest_run_worker: str | None = None
    latest_run_requested_permission: str | None = None
    pending_interaction_count: int = 0
    last_error: str | None = None
    approval_status: Literal["pending", "approved", "rejected", "not_required"] | None = None
    approval_type: str | None = None
    approval_reason: str | None = None
    trace_id: str | None = None
    trace_url: str | None = None


class HumanInteractionSnapshot(ExecutionModel):
    """A pending or resolved human interaction associated with a task."""

    interaction_id: str
    interaction_type: str
    status: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    response_data: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class TaskSnapshot(TaskSummarySnapshot):
    """The full task view with execution history and timeline."""

    task_spec: TaskSpec | None = None
    latest_run: WorkerRunSnapshot | None = None
    pending_interactions: list[HumanInteractionSnapshot] = Field(default_factory=list)
    timeline: list[TaskTimelineEventSnapshot] = Field(default_factory=list)


class OperationalMetrics(ExecutionModel):
    """Aggregated operational metrics for the service (T-092)."""

    total_tasks: int
    retried_tasks: int
    retry_rate: float
    status_counts: dict[str, int]
    worker_usage: dict[str, int]
    runtime_mode_usage: dict[str, int]
    legacy_tool_loop_usage: dict[str, int]
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
    task_spec: dict[str, Any] | None = None
    trace_context: dict[str, str] = field(default_factory=dict)


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


ProgressPhase = Literal["started", "running", "completed", "failed", "awaiting_approval"]


@dataclass(frozen=True)
class ProgressEvent:
    """A task lifecycle update emitted by the execution service."""

    phase: ProgressPhase
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
_RESERVED_INTERNAL_CONSTRAINT_KEYS: frozenset[str] = frozenset(
    {"approval", "worker_profile_override"}
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


def _sanitize_submission_constraints(constraints: Mapping[str, Any]) -> dict[str, Any]:
    """Drop reserved control-plane keys that callers must not set directly."""
    sanitized = dict(constraints)
    for key in _RESERVED_INTERNAL_CONSTRAINT_KEYS:
        sanitized.pop(key, None)
    return sanitized


def _enum_value(value: object | None) -> str | None:
    """Normalize enum-backed ORM values into plain strings."""
    if value is None:
        return None
    member_value = getattr(value, "value", None)
    if isinstance(member_value, str):
        return member_value
    return str(value)


def _task_status_from_result(state: OrchestratorState) -> TaskStatus:
    """Map the final orchestrator result into a persisted task status."""
    if state.approval.required and state.approval.status == "pending":
        # If we interrupted for manual approval, the task is still valid and pollable.
        return TaskStatus.PENDING

    if state.result is None:
        return TaskStatus.FAILED
    if state.result.status == "success":
        return TaskStatus.COMPLETED
    return TaskStatus.FAILED


def _worker_run_status_from_result(state: OrchestratorState) -> WorkerRunStatus:
    """Map the final worker result into a persisted worker-run status."""
    if state.result is None:
        return WorkerRunStatus.ERROR
    if state.result.status == "success":
        return WorkerRunStatus.SUCCESS
    if state.result.status == "failure":
        return WorkerRunStatus.FAILURE
    return WorkerRunStatus.ERROR


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
    requested_permission_level = coerce_permission_level(first.get("requested_permission"))
    requested_permission = (
        requested_permission_level.value if requested_permission_level is not None else None
    )

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


def _extract_graph_payload(data: Any) -> Mapping[str, Any]:
    """Safely extract a mapping payload from a graph state object or model."""
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    if isinstance(data, Mapping):
        return data
    return {}


def _summarize_graph_span_input(graph_input: Mapping[str, Any]) -> dict[str, Any]:
    """Build a compact graph span input payload to avoid emitting full task state."""
    task = _extract_graph_payload(graph_input.get("task"))
    session = _extract_graph_payload(graph_input.get("session"))
    task_spec = _extract_graph_payload(graph_input.get("task_spec"))
    budget = _extract_graph_payload(task.get("budget"))

    summary: dict[str, Any] = {
        "task_id": task.get("task_id"),
        "attempt_count": graph_input.get("attempt_count"),
        "channel": session.get("channel"),
        "branch": task.get("branch"),
        "task_type": task_spec.get("task_type"),
        "execution_mode": task.get("constraints", {}).get("execution_mode")
        if isinstance(task.get("constraints"), Mapping)
        else None,
        "max_iterations": budget.get("max_iterations"),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _summarize_graph_span_output(raw_output: object) -> dict[str, Any]:
    """Build a compact graph span output payload to avoid large span attributes."""
    payload = _extract_graph_payload(raw_output)
    if not payload and not isinstance(raw_output, Mapping | BaseModel):
        return {"output_type": type(raw_output).__name__}

    result = _extract_graph_payload(payload.get("result"))
    review = _extract_graph_payload(payload.get("review"))
    verification = _extract_graph_payload(payload.get("verification"))
    task = _extract_graph_payload(payload.get("task"))
    constraints = task.get("constraints")
    if not isinstance(constraints, Mapping):
        constraints = {}
    interactions = constraints.get("interactions")
    clarification_round = 0
    clarification_resolved = False
    if isinstance(interactions, Mapping):
        for interaction in interactions.values():
            if not isinstance(interaction, Mapping):
                continue
            if interaction.get("interaction_type") == "clarification":
                clarification_round += 1
                if interaction.get("status") == "resolved":
                    clarification_resolved = True
    verification_items = verification.get("items") if isinstance(verification, Mapping) else None
    delivery_contract_passed = None
    if isinstance(verification_items, list):
        for item in verification_items:
            if not isinstance(item, Mapping):
                continue
            if item.get("label") == "file_changes":
                if item.get("status") == "failed" and item.get("reason_code") in {
                    "incomplete_delivery",
                    "scope_mismatch",
                }:
                    delivery_contract_passed = False
                elif item.get("status") in {"passed", "warning"}:
                    delivery_contract_passed = True

    summary: dict[str, Any] = {
        "current_step": payload.get("current_step"),
        "attempt_count": payload.get("attempt_count"),
        "timeline_persisted_count": payload.get("timeline_persisted_count"),
        "repair_handoff_requested": payload.get("repair_handoff_requested"),
        "result_status": result.get("status"),
        "review_outcome": review.get("outcome"),
        "verification_status": verification.get("status"),
        "verifier_failure_kind": verification.get("failure_kind"),
        "clarification_round": clarification_round or None,
        "clarification_resolved": clarification_resolved if clarification_round else None,
        "delivery_contract_passed": delivery_contract_passed,
        "error_count": (
            len(payload.get("errors", [])) if isinstance(payload.get("errors"), list) else None
        ),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _normalize_orchestrator_graph_output(raw_output: object) -> object:
    """Strip transport-only interrupt keys and map unresolved interrupts to failure output."""

    def _normalize_requested_permission(
        raw_permission: object, *, warning_context: str
    ) -> str | None:
        requested_permission_level = coerce_permission_level(raw_permission)
        if raw_permission is not None and requested_permission_level is None:
            logger.warning(
                f"Ignoring unknown permission level from {warning_context}.",
                extra={"requested_permission": raw_permission},
            )
        return requested_permission_level.value if requested_permission_level is not None else None

    if isinstance(raw_output, Mapping):
        normalized = dict(raw_output)
    elif isinstance(raw_output, BaseModel):
        normalized = raw_output.model_dump(mode="json")
        model_extra = raw_output.model_extra
        if (
            normalized.get("__interrupt__") is None
            and isinstance(model_extra, Mapping)
            and "__interrupt__" in model_extra
        ):
            normalized["__interrupt__"] = model_extra["__interrupt__"]
        if normalized.get("__interrupt__") is None and hasattr(raw_output, "__interrupt__"):
            normalized["__interrupt__"] = getattr(raw_output, "__interrupt__")
    else:
        return raw_output

    existing_result = normalized.get("result")
    normalized_result: dict[str, Any] | None = None
    if isinstance(existing_result, Mapping):
        normalized_result = dict(existing_result)
    elif isinstance(existing_result, BaseModel):
        normalized_result = existing_result.model_dump(mode="json")

    if normalized_result is not None:
        normalized_result["requested_permission"] = _normalize_requested_permission(
            normalized_result.get("requested_permission"),
            warning_context="result payload",
        )
        normalized["result"] = normalized_result

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
        requested_permission = _normalize_requested_permission(
            first_payload.get("requested_permission"),
            warning_context="interrupt payload",
        )
        normalized["result"] = WorkerResult(
            status="failure",
            summary=_interrupt_summary(payloads),
            requested_permission=requested_permission,
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


def _terminal_follow_up_status(
    *,
    state: OrchestratorState,
    terminal_failure: bool,
) -> TaskStatus:
    """Map terminal follow-up intent to the persisted task status."""
    if not terminal_failure:
        return TaskStatus.IN_PROGRESS
    if state.approval.status == "rejected":
        return TaskStatus.FAILED
    is_clarification_gate = state.current_step in {"generate_task_spec", "await_clarification"}
    if (
        state.approval.status == "pending"
        or (is_clarification_gate and state.task_spec and state.task_spec.requires_clarification)
        or state.current_step
        in {"await_clarification", "await_permission", "await_permission_escalation"}
    ):
        return TaskStatus.PENDING
    return TaskStatus.FAILED


def _completion_progress_phase(task_snapshot: TaskSnapshot) -> ProgressPhase:
    """Map final task state to a user-facing progress phase."""
    if task_snapshot.status == TaskStatus.COMPLETED.value:
        return "completed"
    if (
        task_snapshot.status == TaskStatus.PENDING.value
        and task_snapshot.approval_status == "pending"
    ):
        return "awaiting_approval"
    return "failed"


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

    def _drop_none_reason_codes(value: Any) -> Any:
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            for key, item in value.items():
                if key == "reason_code" and item is None:
                    continue
                output[str(key)] = _drop_none_reason_codes(item)
            return output
        if isinstance(value, list):
            return [_drop_none_reason_codes(item) for item in value]
        return value

    if hasattr(report, "model_dump"):
        serialized = report.model_dump(mode="json")
        serialized = _drop_none_reason_codes(serialized)
        if serialized.get("failure_kind") is None:
            serialized.pop("failure_kind", None)
        return serialized
    if isinstance(report, Mapping):
        return _drop_none_reason_codes(dict(report))
    raise TypeError(f"Unsupported verification report type: {type(report).__name__}")


def _to_json_compatible(value: object) -> Any:
    """Recursively convert nested model/mapping payloads into JSON-compatible values."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _to_json_compatible(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_json_compatible(item) for item in value]
    return value


def _serialize_review_result(review_result: object | None) -> dict[str, Any] | None:
    """Normalize review output from either a Pydantic model or a raw mapping."""
    if review_result is None:
        return None
    if hasattr(review_result, "model_dump"):
        return review_result.model_dump(mode="json")
    if isinstance(review_result, Mapping):
        return _to_json_compatible(review_result)
    raise TypeError(f"Unsupported review result type: {type(review_result).__name__}")


def _review_result_artifact_entry(
    review_result: object | None,
    *,
    artifact_type: str = ArtifactType.REVIEW_RESULT.value,
) -> dict[str, Any] | None:
    """Build a structured artifact index entry for a review payload when present."""
    serialized = _serialize_review_result(review_result)
    if serialized is None:
        return None
    return {
        "name": artifact_type,
        "uri": f"inline://{artifact_type}",
        "artifact_type": artifact_type,
        "artifact_metadata": {artifact_type: serialized},
    }


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


def _get_trace_id_from_context(context: dict[str, str] | None) -> str | None:
    """Extract the 32-char hex trace ID from a W3C traceparent context."""
    if not context:
        return None
    # Look for W3C traceparent (standard in OTEL)
    traceparent = context.get("traceparent")
    if not traceparent:
        return None
    # format: version-traceid-parentid-flags
    parts = traceparent.split("-")
    if len(parts) >= 2:
        return parts[1]
    return None


# T-152: Global cache for Phoenix project ID to avoid redundant network calls during serialization
PHOENIX_API_TIMEOUT: Final[float] = 2.0
_PHOENIX_PROJECT_ID_CACHE: str | None = None
_PHOENIX_LAST_FAILURE: float = 0
_PHOENIX_FAILURE_TTL: Final[float] = 60.0  # 1 minute
_PHOENIX_PROJECT_ID_LOCK = Lock()


def _get_project_id(api_base_url: str, project_name: str) -> str:
    """Resolve the Phoenix project ID (UUID) from its name via the REST API."""
    global _PHOENIX_PROJECT_ID_CACHE, _PHOENIX_LAST_FAILURE
    if _PHOENIX_PROJECT_ID_CACHE:
        return _PHOENIX_PROJECT_ID_CACHE

    # If resolution failed recently, return the fallback name immediately to avoid blocking
    if time.time() - _PHOENIX_LAST_FAILURE < _PHOENIX_FAILURE_TTL:
        return project_name

    with _PHOENIX_PROJECT_ID_LOCK:
        if _PHOENIX_PROJECT_ID_CACHE:
            return _PHOENIX_PROJECT_ID_CACHE
        if time.time() - _PHOENIX_LAST_FAILURE < _PHOENIX_FAILURE_TTL:
            return project_name

        try:
            # Phoenix API endpoint for project details
            url = f"{api_base_url}/v1/projects/{urllib.parse.quote(project_name)}"
            with urllib.request.urlopen(url, timeout=PHOENIX_API_TIMEOUT) as response:
                data = json.loads(response.read().decode())
                _PHOENIX_PROJECT_ID_CACHE = data["data"]["id"]
        except (urllib.error.URLError, ValueError, KeyError, TypeError, TimeoutError) as e:
            # Fallback to the name if the API is unreachable or the project doesn't exist.
            # Record failure time to implement a TTL before the next retry, preventing
            # performance degradation during task listing.
            logger.debug("Failed to resolve Phoenix project ID for '%s': %s", project_name, e)
            _PHOENIX_LAST_FAILURE = time.time()
            return project_name

        return _PHOENIX_PROJECT_ID_CACHE or project_name


# T-152: Global cache for tracing configuration to avoid redundant env lookups
_TRACING_CONFIG_CACHE: tuple[bool, str | None, str] | None = None
_TRACING_CONFIG_LOCK = Lock()


def _get_tracing_config() -> tuple[bool, str | None, str]:
    """Helper to fetch tracing config once per process."""
    global _TRACING_CONFIG_CACHE
    if _TRACING_CONFIG_CACHE is not None:
        return _TRACING_CONFIG_CACHE

    with _TRACING_CONFIG_LOCK:
        if _TRACING_CONFIG_CACHE is not None:
            return _TRACING_CONFIG_CACHE

        enabled = is_tracing_enabled()
        collector_endpoint = resolve_otel_tracing_endpoint(os.environ)
        project_name = resolve_tracing_project_name(os.environ)
        _TRACING_CONFIG_CACHE = (enabled, collector_endpoint, project_name)
        return _TRACING_CONFIG_CACHE


def _clear_tracing_config_cache() -> None:
    """Internal helper for tests to reset configuration state."""
    global _TRACING_CONFIG_CACHE
    with _TRACING_CONFIG_LOCK:
        _TRACING_CONFIG_CACHE = None


def bootstrap_phoenix_project_id() -> None:
    """Pre-resolve the Phoenix project ID to avoid blocking API threads later."""
    enabled, endpoint, project_name = _get_tracing_config()
    if not enabled or not endpoint:
        return

    # Standard OTLP HTTP traces endpoint to API base URL conversion
    # http://127.0.0.1:6006/v1/traces -> http://127.0.0.1:6006
    api_base_url = endpoint.removesuffix("/v1/traces")
    _get_project_id(api_base_url, project_name)


def _get_phoenix_url(trace_id: str | None) -> str | None:
    """Generate a browser-accessible Phoenix deep link for a given trace ID."""
    if not trace_id:
        return None

    enable_tracing, collector_endpoint, project_name = _get_tracing_config()
    if not enable_tracing:
        return None

    if not collector_endpoint:
        return f"http://localhost:6006/projects/{project_name}/traces/{trace_id}"

    try:
        parsed = urlparse(collector_endpoint)
        # Phoenix API is usually at the same host as the collector, sans /v1/traces
        api_base_url = f"{parsed.scheme}://{parsed.netloc}"
        project_id = _get_project_id(api_base_url, project_name)

        netloc = parsed.netloc
        # If it's the internal service name, swap it for localhost for the browser
        if netloc.startswith("phoenix:"):
            netloc = netloc.replace("phoenix:", "localhost:", 1)
        elif netloc == "phoenix":
            netloc = "localhost:6006"

        # Phoenix UI puts traces at /projects/<id>/traces/<id>
        return f"{parsed.scheme}://{netloc}/projects/{project_id}/traces/{trace_id}"
    except (ValueError, urllib.error.URLError) as e:
        # Fallback if URL parsing or project resolution fails
        logger.debug("Failed to generate custom Phoenix deep link: %s", e)
        return f"http://localhost:6006/projects/{project_name}/traces/{trace_id}"


class TaskExecutionService:
    """Submit tasks through the orchestrator and persist execution-path state."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        worker: Worker,
        gemini_worker: Worker | None = None,
        openrouter_worker: Worker | None = None,
        shell_worker: Worker | None = None,
        worker_profiles: Mapping[str, WorkerProfile] | None = None,
        enable_worker_profiles: bool = False,
        enable_independent_verifier: bool = False,
        orchestrator_brain: OrchestratorBrain | None = None,
        progress_notifier: ProgressNotifier | None = None,
        default_task_max_attempts: int = 3,
        workspace_root: str | Path | None = None,
        retention_seconds: int | None = 7 * 24 * 60 * 60,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.worker = worker
        self.gemini_worker = gemini_worker
        self.openrouter_worker = openrouter_worker
        self.shell_worker = shell_worker
        self.worker_profiles = dict(worker_profiles or {})
        self.enable_worker_profiles = enable_worker_profiles
        self.enable_independent_verifier = enable_independent_verifier
        self.orchestrator_brain = orchestrator_brain
        self.progress_notifier = progress_notifier
        self.default_task_max_attempts = max(1, int(default_task_max_attempts))
        self.workspace_root = None
        if workspace_root is not None:
            self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.retention_seconds = (
            None if retention_seconds is None else max(0, int(retention_seconds))
        )
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
                openrouter_worker=self.openrouter_worker,
                shell_worker=self.shell_worker,
                worker_profiles=self.worker_profiles,
                enable_worker_profiles=self.enable_worker_profiles,
                enable_independent_verifier=self.enable_independent_verifier,
                orchestrator_brain=self.orchestrator_brain,
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

    def _workspace_path_for_run(self, workspace_id: str | None) -> Path | None:
        """Resolve the on-disk workspace path for a retained run, if configured."""
        if workspace_id is None or self.workspace_root is None:
            return None

        workspace_path = (self.workspace_root / workspace_id).resolve()
        try:
            if not workspace_path.is_relative_to(self.workspace_root):
                logger.warning(
                    "Skipping retention cleanup outside the configured workspace root.",
                    extra={
                        "workspace_root": str(self.workspace_root),
                        "workspace_path": str(workspace_path),
                    },
                )
                return None
        except ValueError:
            return None
        return workspace_path

    def _delete_retained_workspace_path(self, workspace_id: str | None) -> bool:
        """Delete a retained workspace directory from disk, if configured."""
        workspace_path = self._workspace_path_for_run(workspace_id)
        if workspace_path is None or not workspace_path.exists():
            return False

        shutil.rmtree(workspace_path)
        return True

    def _prune_retained_runs(self, *, now: datetime) -> int:
        """Delete retained artifact rows and workspace directories for expired runs."""
        if self.retention_seconds is None:
            return 0

        deleted_runs = 0
        with session_scope(self.session_factory) as session:
            worker_run_repo = WorkerRunRepository(session)
            artifact_repo = ArtifactRepository(session)

            for worker_run in worker_run_repo.list_retained_before(now):
                artifact_repo.delete_by_run(worker_run.id)
                worker_run_repo.clear_artifact_index(worker_run.id)
                worker_run.retention_expires_at = None
                if self._delete_retained_workspace_path(worker_run.workspace_id):
                    logger.info(
                        "Deleted retained sandbox workspace",
                        extra={
                            "worker_run_id": worker_run.id,
                            "workspace_id": worker_run.workspace_id,
                        },
                    )
                    deleted_runs += 1

        if deleted_runs:
            logger.info(
                "Pruned retained execution artifacts",
                extra={
                    "deleted_runs": deleted_runs,
                    "workspace_root": str(self.workspace_root) if self.workspace_root else None,
                    "retention_seconds": self.retention_seconds,
                },
            )
        return deleted_runs

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
        submission = self._normalize_and_validate_submission(submission)
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

    def _normalize_and_validate_submission(self, submission: TaskSubmission) -> TaskSubmission:
        """Normalize execution overrides and validate profile selections before persistence."""
        normalized_profile_override = (
            submission.worker_profile_override.strip()
            if isinstance(submission.worker_profile_override, str)
            and submission.worker_profile_override.strip()
            else None
        )
        if (
            normalized_profile_override is not None
            and self.enable_worker_profiles
            and normalized_profile_override not in self.worker_profiles
        ):
            raise TaskSubmissionValidationError(
                "worker_profile_override must reference a configured worker profile "
                f"when profile routing is enabled. Unknown profile: "
                f"'{normalized_profile_override}'."
            )
        if normalized_profile_override == submission.worker_profile_override:
            return submission
        return submission.model_copy(
            update={"worker_profile_override": normalized_profile_override}
        )

    async def submit_task(
        self,
        submission: TaskSubmission,
        persisted: _PersistedTaskContext,
    ) -> None:
        """Legacy direct execution entrypoint kept for compatibility/tests."""
        span_cm = start_optional_span(
            tracer_name="orchestrator.execution",
            span_name="TaskExecutionService.submit_task",
            attributes=with_span_kind(SPAN_KIND_AGENT),
            task_id=persisted.task_id,
            session_id=persisted.session_id,
            channel=persisted.channel,
        )
        with span_cm:
            set_span_input_output(input_data=submission.task_text)
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
                self._update_span_status_from_state(state)
            except Exception as exc:
                self._record_execution_span_error(exc)
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
                        "Failed to reload task snapshot after marking a "
                        "background task as failed",
                        extra={
                            "session_id": persisted.session_id,
                            "task_id": persisted.task_id,
                        },
                    )
                    await self._emit_progress(
                        submission,
                        persisted,
                        phase="failed",
                        summary=(
                            "Task execution failed and the final snapshot " "could not be reloaded."
                        ),
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
                phase=_completion_progress_phase(task_snapshot),
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
        with with_restored_trace_context(persisted.trace_context):
            span_cm = start_optional_span(
                tracer_name="orchestrator.execution",
                span_name="TaskExecutionService.run_queued_task",
                attributes=with_span_kind(SPAN_KIND_AGENT),
                task_id=persisted.task_id,
                session_id=persisted.session_id,
                channel=persisted.channel,
            )
            with span_cm:
                set_current_span_attribute(ATTR_WORKER_ID, worker_id)
                set_span_input_output(input_data=submission.task_text)
                await self._emit_progress(submission, persisted, phase="started")
                await self._emit_progress(submission, persisted, phase="running")

                started_at = utc_now()
                try:
                    orchestrator_task = asyncio.create_task(
                        self._run_orchestrator(submission, persisted),
                        name=f"orchestrator-{task_id}",
                    )
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(
                            task_id=task_id,
                            worker_id=worker_id,
                            lease_seconds=lease_seconds,
                        ),
                        name=f"task-heartbeat-{task_id}",
                    )

                    done, pending = await asyncio.wait(
                        [orchestrator_task, heartbeat_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if orchestrator_task in done:
                        state = orchestrator_task.result()
                    else:
                        # Heartbeat task finished first, which means the lease was lost
                        # or the task status was externally updated (e.g. CANCELLED).
                        # We must cancel the orchestrator task immediately.
                        orchestrator_task.cancel()
                        try:
                            await orchestrator_task
                        except asyncio.CancelledError:
                            # Re-load the task to see why we were cancelled
                            task_snapshot = await self._run_blocking(self.get_task, task_id)
                            is_cancelled = task_snapshot and (
                                task_snapshot.status == TaskStatus.CANCELLED
                                or (
                                    task_snapshot.status == TaskStatus.FAILED
                                    and task_snapshot.last_error == "Task cancelled by operator."
                                )
                            )
                            if is_cancelled:
                                logger.info(
                                    "Task execution aborted: task was cancelled",
                                    extra={"task_id": task_id},
                                )
                                # We don't need to persist outcome here as cancel_task handled it.
                                return None
                            else:
                                logger.warning(
                                    "Task execution aborted: lease lost or stolen",
                                    extra={"task_id": task_id},
                                )
                                # Task was likely stolen or lease expired.
                                # Do NOT release failure as we no longer own the task.
                                return None

                        # If await orchestrator_task didn't raise CancelledError, it means the task
                        # finished before the cancellation took effect. We should still abort
                        # because the heartbeat failed, and not proceed with the result.
                        logger.warning(
                            "Orchestrator task completed despite cancellation request. "
                            "Aborting due to heartbeat failure.",
                            extra={"task_id": task_id},
                        )
                        return None

                    finished_at = utc_now()
                    self._update_span_status_from_state(state)

                    if state.result is not None and state.result.status == "success":
                        await self._run_blocking(
                            self._persist_execution_outcome,
                            task_id=persisted.task_id,
                            state=state,
                            started_at=started_at,
                            finished_at=finished_at,
                            force_task_status=TaskStatus.COMPLETED,
                        )
                        await self._run_blocking(
                            self._release_task_success, task_id=persisted.task_id
                        )
                    else:
                        terminal_failure = _requires_manual_follow_up(state)
                        terminal_status = _terminal_follow_up_status(
                            state=state,
                            terminal_failure=terminal_failure,
                        )
                        await self._run_blocking(
                            self._persist_execution_outcome,
                            task_id=persisted.task_id,
                            state=state,
                            started_at=started_at,
                            finished_at=finished_at,
                            force_task_status=terminal_status,
                        )
                        if terminal_failure:
                            await self._run_blocking(
                                self._release_task_terminal_failure,
                                task_id=persisted.task_id,
                                worker_id=worker_id,
                                status=terminal_status,
                            )
                        else:
                            await self._run_blocking(
                                self._release_task_failure,
                                task_id=persisted.task_id,
                                worker_id=worker_id,
                            )
                except Exception as exc:
                    self._record_execution_span_error(exc)
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
                    raise RuntimeError(
                        f"Persisted task '{persisted.task_id}' could not be reloaded."
                    )
                self._log_task_outcome(task_snapshot)
                await self._emit_progress(
                    submission,
                    persisted,
                    phase=_completion_progress_phase(task_snapshot),
                    summary=self._task_summary(task_snapshot),
                )
        return None

    def _update_span_status_from_state(self, state: OrchestratorState) -> None:
        """Update the current span status based on the orchestrator state outcomes."""
        if "blocked_on_clarification" in state.errors:
            set_span_status_from_outcome("blocked_on_clarification", "awaiting clarification")
        elif state.errors:
            set_span_status_from_outcome("error", state.errors[0])
        elif state.result is not None:
            set_span_status_from_outcome(state.result.status, state.result.summary)

    def _record_execution_span_error(self, exc: Exception) -> None:
        """Log and record a span error for a task execution failure."""
        logger.debug(f"Task execution failed: {exc}", exc_info=True)
        record_span_exception(exc)
        set_span_status_from_outcome("error", str(exc))

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
                logger.debug(
                    "Heartbeat failed: lease lost or task status changed",
                    extra={"task_id": task_id, "worker_id": worker_id},
                )
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
        """Load the current persisted task state with full timeline and latest run (T-131)."""
        with session_scope(self.session_factory) as session:
            statement = (
                select(Task)
                .where(Task.id == task_id)
                .options(
                    selectinload(Task.timeline_events),
                    selectinload(Task.human_interactions),
                    selectinload(Task.worker_runs).selectinload(WorkerRun.artifacts),
                )
            )
            task = session.scalar(statement)
            if task is None:
                return None
            return self._map_task_to_snapshot(task)

    def list_tasks(
        self,
        *,
        session_id: str | None = None,
        status: str | TaskStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TaskSummarySnapshot]:
        """List tasks with optional filtering and pagination using summary views (T-131)."""
        with session_scope(self.session_factory) as session:
            task_repo = TaskRepository(session)
            tasks = task_repo.list_all(
                session_id=session_id,
                status=status,
                limit=limit,
                offset=offset,
                # Optimization: do not load full timeline/runs history for listing
                preload_history=False,
            )
            return [self._map_task_to_summary(task) for task in tasks]

    def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionSnapshot]:
        """List sessions with pagination."""
        with session_scope(self.session_factory) as session:
            session_repo = SessionRepository(session)
            sessions = session_repo.list_all(limit=limit, offset=offset)
            return [self._map_session_to_snapshot(s) for s in sessions]

    def get_session(self, session_id: str) -> SessionSnapshot | None:
        """Load the current persisted session state."""
        with session_scope(self.session_factory) as session:
            session_repo = SessionRepository(session)
            s = session_repo.get(session_id)
            if s is None:
                return None
            return self._map_session_to_snapshot(s)

    def list_personal_memory(
        self,
        *,
        user_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PersonalMemorySnapshot]:
        """List persisted personal memory entries with optional user filtering."""
        with session_scope(self.session_factory) as session:
            memory_repo = PersonalMemoryRepository(session)
            memories = memory_repo.list_all(user_id=user_id, limit=limit, offset=offset)
            return [self._map_personal_memory_to_snapshot(memory) for memory in memories]

    def upsert_personal_memory(
        self,
        payload: PersonalMemoryUpsertRequest,
    ) -> PersonalMemorySnapshot:
        """Create or update one personal memory entry."""
        with session_scope(self.session_factory) as session:
            memory_repo = PersonalMemoryRepository(session)
            upsert_kwargs: dict[str, Any] = {
                "user_id": payload.user_id,
                "memory_key": payload.memory_key,
                "value": payload.value,
            }
            for field_name in (
                "source",
                "confidence",
                "scope",
                "last_verified_at",
                "requires_verification",
            ):
                if field_name in payload.model_fields_set:
                    upsert_kwargs[field_name] = getattr(payload, field_name)

            memory = memory_repo.upsert(**upsert_kwargs)
            return self._map_personal_memory_to_snapshot(memory)

    def delete_personal_memory(self, *, user_id: str, memory_key: str) -> bool:
        """Delete one personal memory entry by key."""
        with session_scope(self.session_factory) as session:
            memory_repo = PersonalMemoryRepository(session)
            return memory_repo.delete(user_id=user_id, memory_key=memory_key)

    def list_project_memory(
        self,
        *,
        repo_url: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProjectMemorySnapshot]:
        """List persisted project memory entries with optional repo filtering."""
        with session_scope(self.session_factory) as session:
            memory_repo = ProjectMemoryRepository(session)
            memories = memory_repo.list_all(repo_url=repo_url, limit=limit, offset=offset)
            return [self._map_project_memory_to_snapshot(memory) for memory in memories]

    def upsert_project_memory(
        self,
        payload: ProjectMemoryUpsertRequest,
    ) -> ProjectMemorySnapshot:
        """Create or update one project memory entry."""
        with session_scope(self.session_factory) as session:
            memory_repo = ProjectMemoryRepository(session)
            upsert_kwargs: dict[str, Any] = {
                "repo_url": payload.repo_url,
                "memory_key": payload.memory_key,
                "value": payload.value,
            }
            for field_name in (
                "source",
                "confidence",
                "scope",
                "last_verified_at",
                "requires_verification",
            ):
                if field_name in payload.model_fields_set:
                    upsert_kwargs[field_name] = getattr(payload, field_name)

            memory = memory_repo.upsert(**upsert_kwargs)
            return self._map_project_memory_to_snapshot(memory)

    def delete_project_memory(self, *, repo_url: str, memory_key: str) -> bool:
        """Delete one project memory entry by key."""
        with session_scope(self.session_factory) as session:
            memory_repo = ProjectMemoryRepository(session)
            return memory_repo.delete(repo_url=repo_url, memory_key=memory_key)

    def _map_task_to_snapshot(self, task: Task) -> TaskSnapshot:
        """Map a Task database model to a full TaskSnapshot Pydantic model (T-131)."""
        latest_run_snapshot: WorkerRunSnapshot | None = None
        latest_run_obj: WorkerRun | None = None
        pending_interactions = self._pending_interaction_snapshots(task)

        if task.worker_runs:
            latest_run_obj = max(task.worker_runs, key=lambda r: (r.started_at, r.id))
            latest_run_snapshot = WorkerRunSnapshot(
                run_id=latest_run_obj.id,
                session_id=latest_run_obj.session_id,
                worker_type=_enum_value(latest_run_obj.worker_type) or "unknown",
                worker_profile=latest_run_obj.worker_profile,
                runtime_mode=_enum_value(latest_run_obj.runtime_mode),
                workspace_id=latest_run_obj.workspace_id,
                status=_enum_value(latest_run_obj.status) or WorkerRunStatus.ERROR.value,
                started_at=latest_run_obj.started_at,
                finished_at=latest_run_obj.finished_at,
                summary=latest_run_obj.summary,
                requested_permission=latest_run_obj.requested_permission,
                budget_usage=latest_run_obj.budget_usage,
                verifier_outcome=self._ensure_verifier_outcome_ids(latest_run_obj.verifier_outcome),
                commands_run=[
                    {"id": cmd.get("id") or f"legacy-{idx}", **cmd}
                    for idx, cmd in enumerate(latest_run_obj.commands_run or [])
                    if isinstance(cmd, dict)
                ],
                files_changed_count=latest_run_obj.files_changed_count,
                files_changed=list(latest_run_obj.files_changed or []),
                artifact_index=[
                    {"id": entry.get("id") or entry.get("uri") or f"idx-{idx}", **entry}
                    for idx, entry in enumerate(latest_run_obj.artifact_index or [])
                    if isinstance(entry, dict)
                ],
                artifacts=[
                    ArtifactSnapshot(
                        artifact_id=artifact.id,
                        artifact_type=_enum_value(artifact.artifact_type)
                        or ArtifactType.RESULT_SUMMARY.value,
                        name=artifact.name,
                        uri=artifact.uri,
                        artifact_metadata=artifact.artifact_metadata,
                    )
                    for artifact in (
                        latest_run_obj.artifacts if "artifacts" in latest_run_obj.__dict__ else []
                    )
                ],
            )

        summary = self._map_task_to_summary(task, latest_run=latest_run_obj)

        return TaskSnapshot(
            **summary.model_dump(),
            task_spec=TaskSpec.model_validate(task.task_spec)
            if isinstance(task.task_spec, Mapping)
            else None,
            latest_run=latest_run_snapshot,
            pending_interactions=pending_interactions,
            timeline=[
                TaskTimelineEventSnapshot(
                    id=event.id,
                    event_type=_enum_value(event.event_type) or "unknown",
                    attempt_number=event.attempt_number,
                    sequence_number=event.sequence_number,
                    message=event.message,
                    payload=event.payload,
                    created_at=event.created_at,
                )
                for event in (task.timeline_events if "timeline_events" in task.__dict__ else [])
            ],
        )

    def _map_task_to_summary(
        self,
        task: Task,
        *,
        latest_run: WorkerRun | None = None,
    ) -> TaskSummarySnapshot:
        """Map a Task database model to a lightweight TaskSummarySnapshot (T-131)."""
        latest_run_id = getattr(task, "_latest_run_id", None)
        latest_run_status = _enum_value(getattr(task, "_latest_run_status", None))
        latest_run_worker = _enum_value(getattr(task, "_latest_run_worker", None))
        latest_run_requested_permission = getattr(task, "_latest_run_requested_permission", None)
        pending_interaction_count = getattr(task, "_pending_interaction_count", None)

        # Fallback if metadata not pre-identified (e.g. from get_task or create_task)
        if latest_run_id is None:
            if latest_run:
                run = latest_run
                latest_run_id = run.id
                latest_run_status = _enum_value(run.status)
                latest_run_worker = _enum_value(run.worker_type)
                latest_run_requested_permission = run.requested_permission
            # Only check task.worker_runs if it's already loaded to avoid N+1 lazy loads in listing
            elif "worker_runs" in task.__dict__ and task.worker_runs:
                run = max(task.worker_runs, key=lambda r: (r.started_at, r.id))
                latest_run_id = run.id
                latest_run_status = _enum_value(run.status)
                latest_run_worker = _enum_value(run.worker_type)
                latest_run_requested_permission = run.requested_permission

        if pending_interaction_count is None:
            if "human_interactions" in task.__dict__:
                pending_interaction_count = self._count_pending_interactions(task)
            else:
                pending_interaction_count = 0

        # Extract approval context from task constraints (T-134)
        constraints = task.constraints or {}
        approval_checkpoint = constraints.get("approval")
        approval_status = None
        approval_type = None
        approval_reason = None
        if isinstance(approval_checkpoint, Mapping):
            approval_status = approval_checkpoint.get("status")
            approval_type = approval_checkpoint.get("approval_type")
            approval_reason = approval_checkpoint.get("reason")

        trace_id = _get_trace_id_from_context(task.trace_context)
        return TaskSummarySnapshot(
            task_id=task.id,
            session_id=task.session_id,
            status=_enum_value(task.status) or TaskStatus.FAILED.value,
            task_text=task.task_text,
            repo_url=task.repo_url,
            branch=task.branch,
            priority=task.priority,
            chosen_worker=_enum_value(task.chosen_worker),
            chosen_profile=task.chosen_profile,
            runtime_mode=_enum_value(task.runtime_mode),
            route_reason=task.route_reason,
            created_at=task.created_at,
            updated_at=task.updated_at,
            latest_run_id=latest_run_id,
            latest_run_status=latest_run_status,
            latest_run_worker=latest_run_worker,
            latest_run_requested_permission=latest_run_requested_permission,
            pending_interaction_count=int(pending_interaction_count or 0),
            last_error=task.last_error,
            approval_status=approval_status,  # type: ignore[arg-type]
            approval_type=approval_type,
            approval_reason=approval_reason,
            trace_id=trace_id,
            trace_url=_get_phoenix_url(trace_id),
        )

    @staticmethod
    def _is_pending_interaction(interaction: HumanInteraction) -> bool:
        return _enum_value(interaction.status) == HumanInteractionStatus.PENDING.value

    @staticmethod
    def _map_human_interaction_snapshot(
        interaction: HumanInteraction,
    ) -> HumanInteractionSnapshot:
        return HumanInteractionSnapshot(
            interaction_id=interaction.id,
            interaction_type=_enum_value(interaction.interaction_type) or "unknown",
            status=_enum_value(interaction.status) or "unknown",
            summary=interaction.summary,
            data=dict(interaction.data or {}),
            response_data=(
                dict(interaction.response_data or {})
                if interaction.response_data is not None
                else None
            ),
            created_at=interaction.created_at,
            updated_at=interaction.updated_at,
        )

    def _pending_interaction_snapshots(self, task: Task) -> list[HumanInteractionSnapshot]:
        pending_interactions = [
            interaction
            for interaction in (
                task.human_interactions if "human_interactions" in task.__dict__ else []
            )
            if self._is_pending_interaction(interaction)
        ]
        return [
            self._map_human_interaction_snapshot(interaction)
            for interaction in sorted(
                pending_interactions, key=lambda row: (row.created_at, row.id)
            )
        ]

    def _count_pending_interactions(self, task: Task) -> int:
        return sum(
            1
            for interaction in task.human_interactions
            if self._is_pending_interaction(interaction)
        )

    def _ensure_verifier_outcome_ids(self, outcome: Any) -> Any:
        """Inject stable IDs into verifier outcome items if missing (T-161)."""
        if not isinstance(outcome, dict):
            return outcome
        items = outcome.get("items")
        if not isinstance(items, list):
            return outcome

        new_items = []
        for idx, item in enumerate(items):
            if isinstance(item, dict) and not item.get("id"):
                # Use a stable combination for old data, fallback to index
                label = item.get("label", "item")
                status = item.get("status", "unknown")
                new_item = {"id": f"v-{idx}-{label}-{status}", **item}
                new_items.append(new_item)
            else:
                new_items.append(item)

        return {**outcome, "items": new_items}

    def _map_session_to_snapshot(self, s: ConversationSession) -> SessionSnapshot:
        """Map a ConversationSession database model to a SessionSnapshot Pydantic model (T-131)."""
        working_context: SessionWorkingContextSnapshot | None = None
        if "session_state" in s.__dict__ and s.session_state is not None:
            state = s.session_state
            working_context = SessionWorkingContextSnapshot(
                active_goal=state.active_goal,
                decisions_made=dict(state.decisions_made or {}),
                identified_risks=dict(state.identified_risks or {}),
                files_touched=list(state.files_touched or []),
                updated_at=state.updated_at,
            )

        return SessionSnapshot(
            session_id=s.id,
            user_id=s.user_id,
            channel=s.channel,
            external_thread_id=s.external_thread_id,
            active_task_id=s.active_task_id,
            status=_enum_value(s.status) or "active",
            last_seen_at=s.last_seen_at,
            created_at=s.created_at,
            updated_at=s.updated_at,
            working_context=working_context,
        )

    @staticmethod
    def _map_personal_memory_to_snapshot(memory: PersonalMemory) -> PersonalMemorySnapshot:
        return PersonalMemorySnapshot(
            memory_id=memory.id,
            user_id=memory.user_id,
            memory_key=memory.memory_key,
            value=dict(memory.value or {}),
            source=memory.source,
            confidence=memory.confidence,
            scope=memory.scope,
            last_verified_at=memory.last_verified_at,
            requires_verification=memory.requires_verification,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
        )

    @staticmethod
    def _map_project_memory_to_snapshot(memory: ProjectMemory) -> ProjectMemorySnapshot:
        return ProjectMemorySnapshot(
            memory_id=memory.id,
            repo_url=memory.repo_url,
            memory_key=memory.memory_key,
            value=dict(memory.value or {}),
            source=memory.source,
            confidence=memory.confidence,
            scope=memory.scope,
            last_verified_at=memory.last_verified_at,
            requires_verification=memory.requires_verification,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
        )

    def record_interaction_response(
        self,
        task_id: str,
        interaction_id: str,
        response: InteractionResponse,
    ) -> TaskSnapshot | None:
        """Apply an operator response to a pending interaction and trigger task resumption."""
        with session_scope(self.session_factory) as session:
            task_repo = TaskRepository(session)
            interaction_repo = HumanInteractionRepository(session)
            timeline_repo = TaskTimelineRepository(session)

            task = task_repo.get(task_id)
            if not task:
                return None

            interaction, applied = interaction_repo.record_response(
                interaction_id=interaction_id,
                task_id=task_id,
                response_data=response.response_data,
                status=response.status,
            )
            if interaction is None:
                return None

            if applied and interaction.status == HumanInteractionStatus.RESOLVED:
                # 1. Update task constraints with the resolved interaction signal.
                content_hash = compute_interaction_content_hash(
                    interaction.interaction_type,
                    interaction.summary,
                    interaction.data,
                )
                constraints = dict(task.constraints or {})
                interactions = dict(constraints.get("interactions") or {})
                interactions[content_hash] = {
                    "status": "resolved",
                    "response_data": response.response_data,
                    "interaction_id": interaction.id,
                    "interaction_type": interaction.interaction_type,
                    "summary": interaction.summary,
                    "data": dict(interaction.data or {}) if interaction.data is not None else {},
                }
                constraints["interactions"] = interactions

                # 2. Unified Approval Satisfaction: if this was a permission grant,
                # also satisfy the legacy approval gate to avoid double-pausing.
                if interaction.interaction_type == HumanInteractionType.PERMISSION:
                    constraints["requires_approval"] = False
                    constraints["approval"] = {
                        "status": "approved",
                        "source": "orchestrator",
                        "reason": f"Permission granted via interaction {interaction.id}",
                        "granted_at": utc_now().isoformat(),
                    }

                task.constraints = constraints

                # 3. Reset next_attempt_at to trigger immediate worker pick-up.
                task.next_attempt_at = utc_now()
                task.status = TaskStatus.PENDING

                # 4. Emit timeline event.
                event_type = (
                    TimelineEventType.APPROVAL_GRANTED
                    if interaction.interaction_type == HumanInteractionType.PERMISSION
                    else TimelineEventType.TASK_SPEC_GENERATED
                )
                timeline_repo.create_next_for_attempt(
                    task_id=task_id,
                    attempt_number=task.attempt_count,
                    event_type=event_type,
                    message=f"Interaction '{interaction.interaction_type}' resolved by operator.",
                    payload={
                        "interaction_id": interaction.id,
                        "response_data": response.response_data,
                    },
                )

            session.flush()
            return self.get_task(task_id)

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

    def cancel_task(self, *, task_id: str) -> TaskSnapshot | None:
        """Terminally cancel a task and record the lifecycle event."""
        with session_scope(self.session_factory) as session:
            task_repo = TaskRepository(session)
            timeline_repo = TaskTimelineRepository(session)

            task, was_cancelled = task_repo.cancel(task_id=task_id)
            if task is None:
                return None

            if was_cancelled:
                timeline_repo.create_next_for_attempt(
                    task_id=task_id,
                    attempt_number=task.attempt_count,
                    event_type=TimelineEventType.TASK_CANCELLED,
                    message="Task was cancelled by operator.",
                )
        return self.get_task(task_id)

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
                runtime_mode_usage=run_metrics["runtime_mode_usage"],
                legacy_tool_loop_usage=run_metrics["legacy_tool_loop_usage"],
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
            if replay_request.worker_profile_override is not None:
                updates["worker_profile_override"] = replay_request.worker_profile_override
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

        if "constraints" in updates:
            updates["constraints"] = _sanitize_submission_constraints(updates["constraints"])

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
                "worker_profile_override": submission.worker_profile_override,
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
            interaction_repo = HumanInteractionRepository(session)
            sanitized_constraints = _sanitize_submission_constraints(submission.constraints)
            persisted_constraints = dict(sanitized_constraints)
            if (
                isinstance(submission.worker_profile_override, str)
                and submission.worker_profile_override.strip()
            ):
                persisted_constraints["worker_profile_override"] = (
                    submission.worker_profile_override.strip()
                )
            if submission.tools is not None:
                persisted_constraints["tools"] = submission.tools
            task_spec = build_task_spec(
                task_text=submission.task_text,
                repo_url=submission.repo_url,
                target_branch=submission.branch,
                constraints=sanitized_constraints,
            ).model_dump(mode="json")

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

            trace_context = capture_trace_context()
            task = task_repo.create(
                session_id=conversation_session.id,
                task_text=submission.task_text,
                repo_url=submission.repo_url,
                branch=submission.branch,
                callback_url=submission.callback_url,
                worker_override=submission.worker_override,
                budget=dict(submission.budget),
                secrets=dict(submission.secrets),
                task_spec=task_spec,
                trace_context=trace_context,
                # Store request-level profile overrides in constraints for now to avoid an
                # immediate schema migration; promoting this to a first-class DB column is a
                # known follow-up so profile selections remain queryable/indexable at scale.
                constraints=persisted_constraints,
                secrets_encrypted=self.is_secret_encryption_active(),
                status=status,
                max_attempts=max(1, max_attempts),
                next_attempt_at=now,
                priority=submission.priority,
            )
            interaction_repo.sync_task_spec_flags(task_id=task.id, task_spec=task_spec)
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
                task_spec=task_spec,
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
        phase: ProgressPhase,
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

        effective_budget = _apply_execution_budget_policy(
            channel=persisted.channel,
            constraints=submission.constraints,
            budget=submission.budget,
        )

        span_cm = start_optional_span(
            tracer_name="orchestrator.execution",
            span_name=(
                f"orchestrator.graph.run (Attempt {persisted.attempt_count})"
                if persisted.attempt_count > 1
                else "orchestrator.graph.run"
            ),
            attributes=with_span_kind(SPAN_KIND_AGENT),
            task_id=persisted.task_id,
            session_id=persisted.session_id,
            attempt=persisted.attempt_count,
            channel=persisted.channel,
        )
        with span_cm:
            graph_input = {
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
                    "worker_profile_override": submission.worker_profile_override,
                    "constraints": dict(submission.constraints),
                    "budget": effective_budget,
                    "secrets": dict(submission.secrets),
                    "tools": submission.tools,
                },
                "task_spec": persisted.task_spec,
                "attempt_count": persisted.attempt_count,
                "timeline_persisted_count": initial_persisted_count,
            }

            set_span_input_output(input_data=_summarize_graph_span_input(graph_input))

            raw_output = await self.graph.ainvoke(graph_input, config=config)
            set_span_input_output(
                input_data=None,
                output_data=_summarize_graph_span_output(raw_output),
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
            task_constraints = dict(task.constraints or {})
            worker_profile_override = task_constraints.pop("worker_profile_override", None)
            submission = TaskSubmission(
                task_text=task.task_text,
                repo_url=task.repo_url,
                branch=task.branch,
                worker_override=task.worker_override,
                worker_profile_override=(
                    worker_profile_override.strip()
                    if isinstance(worker_profile_override, str) and worker_profile_override.strip()
                    else None
                ),
                constraints=task_constraints,
                budget=dict(task.budget or {}),
                secrets=dict(task.secrets or {}),
                callback_url=task.callback_url,
                tools=task_constraints.get("tools"),
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
                task_spec=dict(task.task_spec) if isinstance(task.task_spec, dict) else None,
                trace_context=dict(task.trace_context or {}),
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

    def _release_task_terminal_failure(
        self, *, task_id: str, worker_id: str, status: TaskStatus = TaskStatus.FAILED
    ) -> None:
        """Release queue lease while preserving terminal status for manual follow-up."""
        with session_scope(self.session_factory) as session:
            TaskRepository(session).release_terminal_failure(
                task_id=task_id,
                worker_id=worker_id,
                status=status,
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
        """Run a synchronous persistence operation in a worker thread, propagating trace context."""

        def _invoke() -> Any:
            return func(*args, **kwargs)

        wrapped = bind_current_trace_context(_invoke, original_func=func)
        return await to_thread.run_sync(wrapped)

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
        logger.info(
            "Persisting execution outcome",
            extra={
                "task_id": task_id,
                "approval_required": state.approval.required,
                "approval_status": state.approval.status,
                "timeline_count": len(state.timeline_events),
            },
        )
        retention_expires_at = (
            finished_at + timedelta(seconds=self.retention_seconds)
            if self.retention_seconds is not None
            else None
        )
        with session_scope(self.session_factory) as session:
            task_repo = TaskRepository(session)
            interaction_repo = HumanInteractionRepository(session)
            worker_run_repo = WorkerRunRepository(session)
            artifact_repo = ArtifactRepository(session)

            task = task_repo.get(task_id)
            if task is None:
                raise RuntimeError(f"Task '{task_id}' disappeared while persisting execution.")

            if state.route.chosen_worker is not None and state.route.route_reason is not None:
                task.chosen_worker = cast(WorkerType, state.route.chosen_worker)
                task.chosen_profile = state.route.chosen_profile
                task.runtime_mode = cast(WorkerRuntimeMode | None, state.route.runtime_mode)
                task.route_reason = state.route.route_reason

            if state.task_spec is not None:
                task.task_spec = state.task_spec.model_dump(mode="json")
            if isinstance(task.task_spec, Mapping):
                interaction_repo.sync_task_spec_flags(task_id=task_id, task_spec=task.task_spec)

            task.status = cast(TaskStatus, force_task_status or _task_status_from_result(state))

            approval = state.approval
            if approval.required:
                # If approval is required, we always want to reflect its current status
                # in the task constraints so the dashboard can show the banner.
                approval_status = approval.status
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
            review_sources = (
                (
                    result.review_result if result is not None else None,
                    ArtifactType.REVIEW_RESULT.value,
                ),
                (state.review, ArtifactType.INDEPENDENT_REVIEW_RESULT.value),
            )
            review_artifact_entries: list[tuple[str, dict[str, Any]]] = []
            for review_payload, review_artifact_type in review_sources:
                review_entry = _review_result_artifact_entry(
                    review_payload,
                    artifact_type=review_artifact_type,
                )
                if review_entry is None:
                    continue
                artifact_index.append(review_entry)
                review_artifact_entries.append((review_artifact_type, review_entry))
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
                retention_expires_at=retention_expires_at,
                worker_profile=state.route.chosen_profile,
                runtime_mode=state.route.runtime_mode,
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
            for review_artifact_type, review_entry in review_artifact_entries:
                artifact_repo.create(
                    run_id=worker_run.id,
                    artifact_type=review_artifact_type,
                    name=review_entry["name"],
                    uri=review_entry["uri"],
                    artifact_metadata=review_entry["artifact_metadata"],
                )

        self._prune_retained_runs(now=finished_at)

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
