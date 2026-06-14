"""Boundary types for execution-path task orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from db.enums import HumanInteractionStatus, TaskStatus, WorkerType
from orchestrator.execution_policy import validate_callback_url
from orchestrator.state import TaskSpec


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


class SessionWorkingContextSnapshot(ExecutionModel):
    """Compact working context persisted for a session."""

    active_goal: str | None = None
    decisions_made: dict[str, Any] = Field(default_factory=dict)
    identified_risks: dict[str, Any] = Field(default_factory=dict)
    files_touched: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


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


class ProposalSnapshot(ExecutionModel):
    """A persisted idea or code proposal for review."""

    proposal_id: str
    session_id: str
    task_id: str | None = None
    title: str
    summary: str
    content: str | None = None
    status: str
    metadata_payload: dict[str, Any] = Field(default_factory=dict)
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
    last_run_dispatch: dict[str, Any] | None = None
    last_run_result: dict[str, Any] | None = None
    timeline_events: list[dict[str, Any]] = field(default_factory=list)


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


REPLAYABLE_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
)
RESERVED_INTERNAL_CONSTRAINT_KEYS: frozenset[str] = frozenset(
    {"approval", "worker_profile_override"}
)


@dataclass(frozen=True)
class TaskReplayResult:
    """Outcome of replaying a prior task."""

    status: Literal["created", "not_found", "not_replayable"]
    task_snapshot: TaskSnapshot | None = None
    source_task_id: str | None = None
    detail: str | None = None
