"""HTTP schemas for task submission and preprocessing boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from db.enums import WorkerType
from orchestrator.execution import SubmissionSession, TaskReplayRequest, validate_callback_url
from sandbox.secrets import SecretRef


class ScoutTriggerRequest(BaseModel):
    """Payload for manually triggering a scout task."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["repo", "research", "deep"] = "repo"
    repo_key: str | None = None
    branch: str | None = None
    focus: str | None = None
    depth: Literal["shallow", "standard", "deep"] = "standard"
    max_proposals: int = Field(default=5)

    @field_validator("repo_key", "branch", "focus", mode="after")
    @classmethod
    def _normalize_blank_strings(cls, v: str | None) -> str | None:
        if v is not None:
            return v.strip() or None
        return v

    @field_validator("max_proposals", mode="after")
    @classmethod
    def _clamp_max_proposals(cls, v: int) -> int:
        return max(1, min(20, v))

    @model_validator(mode="after")
    def _validate_research_mode(self) -> ScoutTriggerRequest:
        if self.mode == "research" and not self.focus:
            raise ValueError("Research mode requires a focus topic.")
        return self


class CreateTaskRequest(BaseModel):
    """Public HTTP payload for submitting a new task.

    Validates inputs and resolves repository references before mapping
    to the internal TaskSubmission model.
    """

    model_config = ConfigDict(extra="forbid")

    task_text: str = Field(min_length=1)
    repo_key: str | None = Field(default=None, max_length=255)
    branch: str | None = Field(default=None, max_length=255)
    priority: int = Field(default=0, ge=0)
    worker_override: WorkerType | None = None
    worker_profile_override: str | None = Field(default=None, min_length=1, max_length=255)
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    secret_refs: tuple[SecretRef, ...] = Field(default_factory=tuple)
    tools: list[str] | None = None
    callback_url: str | None = Field(default=None, max_length=2048)
    session: SubmissionSession | None = None

    @field_validator("callback_url")
    @classmethod
    def _validate_callback_url(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_callback_url(v)
        return v


@dataclass
class SanitizedCreateTaskIngress:
    """Pre-sanitized ingress payload for task creation."""

    request: CreateTaskRequest
    raw_secrets: dict[str, str] = field(repr=False)


@dataclass
class SanitizedTaskReplayIngress:
    """Pre-sanitized ingress payload for task replay."""

    request: TaskReplayRequest | None
    raw_secrets: dict[str, str] = field(repr=False)
