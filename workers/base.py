"""Typed worker contract models and interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkerModel(BaseModel):
    """Base model for worker interface boundaries."""

    model_config = ConfigDict(extra="forbid")


class WorkerRequest(WorkerModel):
    """Normalized task input passed from the orchestrator to a worker."""

    session_id: str | None = None
    repo_url: str | None = None
    branch: str | None = None
    task_text: str = Field(min_length=1)
    memory_context: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)


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
    requested_permission: str | None = None
    budget_usage: dict[str, Any] | None = None
    commands_run: list[WorkerCommand] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    test_results: list[TestResult] = Field(default_factory=list)
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    next_action_hint: str | None = None


class Worker(ABC):
    """Shared interface every coding worker must implement."""

    @abstractmethod
    def run(self, request: WorkerRequest) -> Awaitable[WorkerResult]:
        """Execute a task request and return a structured result."""
