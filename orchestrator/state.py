"""Typed state models for orchestrator workflow execution."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from workers import WorkerResult

WorkerType = Literal["claude", "codex"]
MemoryCategory = Literal["personal", "project"]
WorkflowStep = Literal[
    "ingest_task",
    "classify_task",
    "load_memory",
    "choose_worker",
    "check_approval",
    "await_approval",
    "dispatch_job",
    "await_result",
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
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)


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
    route_reason: str | None = None
    override_applied: bool = False


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
    workspace_id: str | None = None


class PersistMemoryEntry(OrchestratorModel):
    """A memory update the orchestrator intends to persist after a run."""

    category: MemoryCategory
    memory_key: str
    value: dict[str, Any]
    repo_url: str | None = None


class OrchestratorState(OrchestratorModel):
    """Top-level state handed between orchestrator workflow nodes."""

    current_step: WorkflowStep = "ingest_task"
    session: SessionRef | None = None
    task: TaskRequest
    normalized_task_text: str | None = None
    task_kind: str | None = None
    memory: MemoryContext = Field(default_factory=MemoryContext)
    route: RouteDecision = Field(default_factory=RouteDecision)
    approval: ApprovalCheckpoint = Field(default_factory=ApprovalCheckpoint)
    dispatch: WorkerDispatch = Field(default_factory=WorkerDispatch)
    result: WorkerResult | None = None
    memory_to_persist: list[PersistMemoryEntry] = Field(default_factory=list)
    progress_updates: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    attempt_count: int = Field(default=0, ge=0)
