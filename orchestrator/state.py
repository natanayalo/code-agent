"""Typed state models for orchestrator workflow execution."""

from __future__ import annotations

from datetime import datetime
from operator import add
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from workers import (
    SUPPORTED_WORKER_TYPES as WORKER_SUPPORTED_TYPES,
)
from workers import (
    WorkerDeliveryMode,
    WorkerResult,
    WorkerRuntimeMode,
    WorkerType,
)
from workers.review import ReviewResult

# Re-export worker fallback order for orchestrator callers that import from this module.
SUPPORTED_WORKER_TYPES: tuple[WorkerType, ...] = WORKER_SUPPORTED_TYPES

MemoryCategory = Literal["personal", "project"]
VerificationFailureKind = Literal[
    "test_regression",
    "scope_mismatch",
    "risky_command",
    "worker_failure",
    "unknown",
]
WorkflowStep = Literal[
    "ingest_task",
    "classify_task",
    "plan_task",
    "generate_task_spec",
    "load_memory",
    "choose_worker",
    "check_approval",
    "await_approval",
    "dispatch_job",
    "await_result",
    "await_permission_escalation",
    "verify_result",
    "review_result",
    "summarize_result",
    "persist_memory",
]


class OrchestratorModel(BaseModel):
    """Base model for orchestrator state boundaries."""

    model_config = ConfigDict(extra="forbid")


class SessionRef(OrchestratorModel):
    """Session context restored before orchestration begins."""

    session_id: str
    user_id: str
    channel: str
    external_thread_id: str
    active_task_id: str | None = None
    status: str = "active"


class TaskRequest(OrchestratorModel):
    """Normalized task request data for a workflow run."""

    task_id: str | None = None
    task_text: str = Field(min_length=1)
    repo_url: str | None = None
    branch: str | None = None
    priority: int = Field(default=0, ge=0)
    worker_override: WorkerType | None = None
    worker_profile_override: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    tools: list[str] | None = None


class MemoryEntry(OrchestratorModel):
    """A structured memory record loaded for a task."""

    memory_key: str
    value: dict[str, Any]


class MemoryContext(OrchestratorModel):
    """Structured memory available to the orchestrator."""

    personal: list[MemoryEntry] = Field(default_factory=list)
    project: list[MemoryEntry] = Field(default_factory=list)
    session: dict[str, Any] = Field(default_factory=dict)


class RouteDecision(OrchestratorModel):
    """Worker routing outcome for the current task."""

    chosen_worker: WorkerType | None = None
    chosen_profile: str | None = None
    runtime_mode: WorkerRuntimeMode | None = None
    route_reason: str | None = None
    override_applied: bool = False


class TaskPlanStep(OrchestratorModel):
    """A single ordered planning step for complex tasks."""

    step_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)


class TaskPlan(OrchestratorModel):
    """Structured decomposition emitted for complex tasks."""

    triggered: bool = False
    complexity_reason: str | None = None
    steps: list[TaskPlanStep] = Field(default_factory=list)


TaskRiskLevel = Literal["low", "medium", "high", "critical"]
TaskSpecType = Literal[
    "docs",
    "bugfix",
    "feature",
    "refactor",
    "investigation",
    "review_fix",
    "maintenance",
]
TaskDeliveryMode = WorkerDeliveryMode


class TaskSpec(OrchestratorModel):
    """Structured task contract generated before worker routing."""

    goal: str = Field(min_length=1)
    repo_url: str | None = None
    target_branch: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    risk_level: TaskRiskLevel = "low"
    task_type: TaskSpecType = "feature"
    allowed_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    requires_clarification: bool = False
    clarification_questions: list[str] = Field(default_factory=list)
    requires_permission: bool = False
    permission_reason: str | None = None
    delivery_mode: TaskDeliveryMode = "workspace"


class ApprovalCheckpoint(OrchestratorModel):
    """Approval state for an interruptible workflow step."""

    required: bool = False
    status: Literal["not_required", "pending", "approved", "rejected"] = "not_required"
    approval_type: str | None = None
    reason: str | None = None
    resume_token: str | None = None


class WorkerDispatch(OrchestratorModel):
    """Tracking information for the current worker dispatch attempt."""

    run_id: str | None = None
    worker_type: WorkerType | None = None
    worker_profile: str | None = None
    runtime_mode: WorkerRuntimeMode | None = None
    workspace_id: str | None = None


class PersistMemoryEntry(OrchestratorModel):
    """A memory update the orchestrator intends to persist after a run."""

    category: MemoryCategory
    memory_key: str
    value: dict[str, Any]
    repo_url: str | None = None
    source: str | None = None
    confidence: float = 1.0
    scope: str | None = None
    last_verified_at: datetime | None = None
    requires_verification: bool = True


class SessionStateUpdate(OrchestratorModel):
    """A compact session state update to be persisted (T-062)."""

    active_goal: str | None = None
    decisions_made: dict[str, Any] | None = None
    identified_risks: dict[str, Any] | None = None
    files_touched: list[str] | None = None


class VerificationReportItem(OrchestratorModel):
    """A single diagnostic result from the verification stage."""

    label: str = Field(min_length=1)
    status: Literal["passed", "failed", "warning"]
    message: str | None = None


class VerificationReport(OrchestratorModel):
    """Summarized outcome of the constrained verification stage."""

    status: Literal["passed", "failed", "warning"]
    summary: str | None = None
    failure_kind: VerificationFailureKind | None = None
    items: list[VerificationReportItem] = Field(default_factory=list)


class TaskTimelineEventState(OrchestratorModel):
    """A granular event in a task's lifecycle captured during orchestration (T-090)."""

    event_type: str
    attempt_number: int = 0
    sequence_number: int = 0
    message: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime | None = None


class HumanInteractionSnapshot(OrchestratorModel):
    """A snapshot of a human interaction checkpoint."""

    interaction_id: str
    interaction_type: str
    status: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    response_data: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class OrchestratorState(OrchestratorModel):
    """Top-level state handed between orchestrator workflow nodes."""

    current_step: WorkflowStep = "ingest_task"
    session: SessionRef | None = None
    task: TaskRequest
    normalized_task_text: str | None = None
    task_kind: str | None = None
    task_plan: TaskPlan | None = None
    task_spec: TaskSpec | None = None
    memory: MemoryContext = Field(default_factory=MemoryContext)
    interactions: list[HumanInteractionSnapshot] = Field(default_factory=list)
    route: RouteDecision = Field(default_factory=RouteDecision)
    approval: ApprovalCheckpoint = Field(default_factory=ApprovalCheckpoint)
    dispatch: WorkerDispatch = Field(default_factory=WorkerDispatch)
    result: WorkerResult | None = None
    verification: VerificationReport | None = None
    review: ReviewResult | None = None
    memory_to_persist: list[PersistMemoryEntry] = Field(default_factory=list)
    progress_updates: list[str] = Field(default_factory=list)
    timeline_events: Annotated[list[TaskTimelineEventState], add] = Field(default_factory=list)
    timeline_persisted_count: int = 0
    repair_handoff_requested: bool = False
    errors: list[str] = Field(default_factory=list)
    attempt_count: int = Field(default=0, ge=0)
    session_state_update: SessionStateUpdate | None = None
