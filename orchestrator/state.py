"""Typed state models for orchestrator workflow execution."""

from __future__ import annotations

from datetime import datetime
from operator import add

# Delay import to avoid circular dependency
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestrator.reflection import FrictionReport
from orchestrator.repo_profile import RepoProfile
from workers import (
    SUPPORTED_WORKER_TYPES as WORKER_SUPPORTED_TYPES,
)
from workers import (
    WorkerDeliveryMode,
    WorkerResult,
    WorkerRuntimeMode,
    WorkerType,
    normalize_worker_type,
)
from workers.review import ReviewResult

if TYPE_CHECKING:
    from orchestrator.repo_profile import RepoProfile

# Re-export worker fallback order for orchestrator callers that import from this module.
SUPPORTED_WORKER_TYPES: tuple[WorkerType, ...] = WORKER_SUPPORTED_TYPES

MemoryCategory = Literal["personal", "project"]
VerificationFailureKind = Literal[
    "test_regression",
    "scope_mismatch",
    "incomplete_delivery",
    "infra_verifier_unavailable",
    "risky_command",
    "worker_failure",
    "timeout",
    "unknown",
]
WorkflowStep = Literal[
    "ingest_task",
    "load_repo_profile",
    "classify_task",
    "plan_task",
    "generate_task_spec_and_route",
    "await_clarification",
    "load_memory",
    "await_permission",
    "check_approval",
    "await_approval",
    "provision_workspace",
    "init_environment",
    "dispatch_job",
    "await_result",
    "transition_to_research_phase",
    "await_permission_escalation",
    "verify_result",
    "review_result",
    "deliver_result",
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

    @field_validator("worker_override", mode="before")
    @classmethod
    def normalize_worker_override(cls, value: Any) -> Any:
        return normalize_worker_type(value)


class MemoryEntry(OrchestratorModel):
    """A structured memory record loaded for a task."""

    memory_key: str
    value: dict[str, Any]
    source: str | None = None
    confidence: float = 1.0
    scope: str | None = None
    last_verified_at: datetime | None = None
    requires_verification: bool = True

    # Read-side gating metadata (M23.9)
    staleness: float = 0.0
    conflict: str | None = None
    risk: str = "low"
    advisory_strength: float = 1.0
    gate_status: str = "accepted"
    gate_reason_codes: list[str] = Field(default_factory=list)


class ObservationContextEntry(OrchestratorModel):
    """An episodic observation record loaded for a task context."""

    id: str
    observed_at: datetime
    source: str
    event_type: str
    summary: str
    privacy_stripped: bool = False


class MemoryContext(OrchestratorModel):
    """Structured memory available to the orchestrator."""

    personal: list[MemoryEntry] = Field(default_factory=list)
    project: list[MemoryEntry] = Field(default_factory=list)
    session: dict[str, Any] = Field(default_factory=dict)
    observations: list[ObservationContextEntry] = Field(default_factory=list)
    gate_diagnostics: dict[str, Any] | None = None


class RouteDecision(OrchestratorModel):
    """Worker routing outcome for the current task."""

    chosen_worker: WorkerType | None = None
    chosen_profile: str | None = None
    runtime_mode: WorkerRuntimeMode | None = None
    route_reason: str | None = None
    override_applied: bool = False
    route_metadata: dict[str, Any] | None = None

    @field_validator("chosen_worker", mode="before")
    @classmethod
    def normalize_chosen_worker(cls, value: Any) -> Any:
        return normalize_worker_type(value)


class TaskPlanStep(OrchestratorModel):
    """A single ordered planning step for complex tasks."""

    step_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    depends_on: list[str] | None = None


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
    "scout",
]
TaskDeliveryMode = WorkerDeliveryMode
TaskWorkspaceMode = Literal["clone", "init", "none"]


class TaskSpec(OrchestratorModel):
    """Structured task contract generated before worker routing."""

    goal: str = Field(min_length=1)
    repo_url: str | None = None
    target_branch: str | None = None
    workspace_mode: TaskWorkspaceMode = "clone"
    setup_commands: list[str] = Field(default_factory=list)
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
    delivery_branch: str | None = None
    pr_title: str | None = None
    pr_body: str | None = None

    @field_validator("delivery_branch", mode="before")
    @classmethod
    def strip_delivery_branch(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v


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
    runtime_manifest: dict[str, Any] | None = None

    @field_validator("worker_type", mode="before")
    @classmethod
    def normalize_worker_type(cls, value: Any) -> Any:
        return normalize_worker_type(value)


class PersistMemoryEntry(OrchestratorModel):
    """A candidate memory the orchestrator will submit to admission after a run."""

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
    reason_code: str | None = None


class VerificationReport(OrchestratorModel):
    """Summarized outcome of the constrained verification stage."""

    status: Literal["passed", "failed", "warning"]
    summary: str | None = None
    failure_kind: VerificationFailureKind | None = None
    items: list[VerificationReportItem] = Field(default_factory=list)
    deterministic_verification: dict[str, Any] | None = None


class TaskTimelineEventState(OrchestratorModel):
    """A granular event in a task's lifecycle captured during orchestration (T-090)."""

    event_type: str
    attempt_number: int = 0
    sequence_number: int = 0
    message: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime | None = None


class ScoutPhaseResult(OrchestratorModel):
    """Result of an intermediate phase in a chained task execution."""

    phase: Literal["repo", "research"]
    result: WorkerResult


class OrchestratorState(OrchestratorModel):
    """Top-level state handed between orchestrator workflow nodes."""

    current_step: WorkflowStep = "ingest_task"
    session: SessionRef | None = None
    task: TaskRequest
    normalized_task_text: str | None = None
    task_kind: str | None = None
    task_plan: TaskPlan | None = None
    task_spec: TaskSpec | None = None
    repo_profile: RepoProfile | None = None
    memory: MemoryContext = Field(default_factory=MemoryContext)
    route: RouteDecision = Field(default_factory=RouteDecision)
    approval: ApprovalCheckpoint = Field(default_factory=ApprovalCheckpoint)
    dispatch: WorkerDispatch = Field(default_factory=WorkerDispatch)
    result: WorkerResult | None = None
    verification: VerificationReport | None = None
    review: ReviewResult | None = None
    friction_reports: list[FrictionReport] = Field(default_factory=list)
    memory_to_persist: list[PersistMemoryEntry] = Field(default_factory=list)
    progress_updates: list[str] = Field(default_factory=list)
    timeline_events: Annotated[list[TaskTimelineEventState], add] = Field(default_factory=list)
    timeline_persisted_count: int = 0
    repair_handoff_requested: bool = False
    errors: list[str] = Field(default_factory=list)
    attempt_count: int = Field(default=0, ge=0)
    session_state_update: SessionStateUpdate | None = None
    scout_phase: Literal["repo", "research"] | None = None
    scout_phase_results: list[ScoutPhaseResult] = Field(default_factory=list)


def is_task_read_only(state: OrchestratorState) -> bool:
    """Determine if a task is read-only based on the TaskSpec contract or legacy constraints."""
    if state.task_spec is not None:
        if state.task_spec.task_type == "scout":
            return True
        if state.task_spec.allowed_actions:
            return "modify_workspace_files" not in state.task_spec.allowed_actions

    constraints = state.task.constraints if isinstance(state.task.constraints, dict) else {}
    return constraints.get("read_only") is True or constraints.get("task_type") == "scout"
