"""Typed worker contract models and interface."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from db.enums import WorkerRuntimeMode
from workers.review import ReviewResult

logger = logging.getLogger(__name__)


class WorkerModel(BaseModel):
    """Base model for worker interface boundaries."""

    model_config = ConfigDict(extra="forbid")


FailureKind = Literal[
    "compile",
    "test",
    "tool_runtime",
    "sandbox_infra",
    "timeout",
    "budget_exceeded",
    "permission_denied",
    "context_window",
    "provider_error",
    "provider_auth",
    "unknown",
]

WorkerType = Literal["gemini", "codex", "openrouter"]
# Ordered by escalation preference in routing fallbacks:
# quality-first, then balanced, then low-cost.
SUPPORTED_WORKER_TYPES: Final[tuple[WorkerType, ...]] = ("gemini", "openrouter", "codex")
WorkerCapabilityTag = Literal["planning", "execution", "review", "routing", "scout"]
WorkerPermissionProfile = Literal[
    "read_only",
    "workspace_write",
    "dangerous_shell",
    "networked_write",
    "git_push_or_deploy",
]
WorkerMutationPolicy = Literal["read_only", "patch_allowed"]
WorkerSelfReviewPolicy = Literal["never", "on_failure", "always"]
WorkerDeliveryMode = Literal["summary", "workspace", "branch", "draft_pr"]


class WorkerRequest(WorkerModel):
    """Normalized task input passed from the orchestrator to a worker."""

    session_id: str | None = None
    repo_url: str | None = None
    branch: str | None = None
    task_text: str = Field(min_length=1)
    memory_context: dict[str, Any] = Field(default_factory=dict)
    task_plan: dict[str, Any] | None = None
    task_spec: dict[str, Any] | None = None
    secrets: dict[str, str] = Field(default_factory=dict)
    tools: list[str] | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    worker_profile: str | None = None
    runtime_mode: WorkerRuntimeMode | None = None


class WorkerProfile(WorkerModel):
    """Typed profile contract used for worker runtime selection and policy."""

    name: str = Field(min_length=1)
    worker_type: WorkerType
    runtime_mode: WorkerRuntimeMode
    capability_tags: list[WorkerCapabilityTag] = Field(default_factory=list)
    default_budget: dict[str, Any] = Field(default_factory=dict)
    permission_profile: WorkerPermissionProfile = "workspace_write"
    mutation_policy: WorkerMutationPolicy = "patch_allowed"
    self_review_policy: WorkerSelfReviewPolicy = "on_failure"
    supported_delivery_modes: list[WorkerDeliveryMode] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("capability_tags", "supported_delivery_modes", mode="before")
    @classmethod
    def _normalize_profile_lists(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, list | tuple | set):
            try:
                return sorted(set(value))
            except TypeError:
                logger.debug("Failed to sort or deduplicate profile list value: %r", value)
                return list(value)
        return value


class WorkerCommand(WorkerModel):
    """A command reported by a worker result."""

    command: str
    exit_code: int | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    stdout_artifact_uri: str | None = None
    stderr_artifact_uri: str | None = None


class TestResult(WorkerModel):
    """A summarized test result emitted by a worker."""

    name: str
    status: Literal["passed", "failed", "skipped", "error"]
    details: str | None = None


class ArtifactReference(WorkerModel):
    """A summarized artifact emitted by a worker run."""

    name: str
    uri: str
    artifact_type: str | None = None


class WorkerResult(WorkerModel):
    """Structured result returned from a coding worker."""

    status: Literal["success", "failure", "error"]
    summary: str | None = None
    failure_kind: FailureKind | None = None
    requested_permission: str | None = None
    budget_usage: dict[str, Any] | None = None
    commands_run: list[WorkerCommand] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    test_results: list[TestResult] = Field(default_factory=list)
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    review_result: ReviewResult | None = None
    diff_text: str | None = None
    next_action_hint: str | None = None

    @model_validator(mode="after")
    def _normalize_failure_kind(self) -> WorkerResult:
        """Ensure non-success outcomes always carry a typed failure kind."""
        if self.status == "success":
            self.failure_kind = None
        elif self.failure_kind is None:
            self.failure_kind = "unknown"
        return self


class Worker(ABC):
    """Shared interface every coding worker must implement."""

    @abstractmethod
    def run(
        self,
        request: WorkerRequest,
        *,
        system_prompt: str | None = None,
    ) -> Awaitable[WorkerResult]:
        """Execute a task request and return a structured result."""
