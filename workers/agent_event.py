"""Provider-neutral typed event stream models for native agent runs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from workers.base import WorkerType
from workers.constants import (
    AGENT_EVENT_MAX_MESSAGE_CHARS,
    AGENT_EVENT_MAX_SUMMARY_CHARS,
    AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS,
)


class AgentEventBase(BaseModel):
    """Shared base metadata present on all normalized agent stream events."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    sequence: int
    timestamp: datetime | None = Field(default_factory=lambda: datetime.now(UTC))
    task_id: str | None = None
    session_id: str | None = None
    worker_type: WorkerType | None = None
    provider_event_type: str | None = None


class AgentStarted(AgentEventBase):
    """Lifecycle event emitted when an agent run begins."""

    event_type: Literal["agent_started"] = "agent_started"
    command: str | None = None


class AgentProgress(AgentEventBase):
    """Interim execution heartbeat or phase transition (reasoning suppressed)."""

    event_type: Literal["agent_progress"] = "agent_progress"
    phase: str | None = None
    message: str | None = Field(default=None, max_length=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS)


class AgentMessage(AgentEventBase):
    """Assistant, user, or system message produced during execution."""

    event_type: Literal["agent_message"] = "agent_message"
    role: Literal["assistant", "user", "system"] = "assistant"
    content: str = Field(max_length=AGENT_EVENT_MAX_MESSAGE_CHARS)


class ToolRequested(AgentEventBase):
    """Tool invocation requested by the agent."""

    event_type: Literal["tool_requested"] = "tool_requested"
    tool_name: str
    call_id: str | None = None
    input_summary: str | None = Field(default=None, max_length=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS)


class ToolCompleted(AgentEventBase):
    """Tool invocation completed."""

    event_type: Literal["tool_completed"] = "tool_completed"
    tool_name: str
    call_id: str | None = None
    exit_code: int | None = None
    output_summary: str | None = Field(default=None, max_length=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS)
    duration_seconds: float | None = None


class FileChanged(AgentEventBase):
    """File touched or modified in the workspace."""

    event_type: Literal["file_changed"] = "file_changed"
    path: str = Field(max_length=500)
    change_kind: Literal["added", "modified", "deleted"] = "modified"


class PermissionRequested(AgentEventBase):
    """Permission escalation or confirmation request."""

    event_type: Literal["permission_requested"] = "permission_requested"
    permission_type: str
    reason: str | None = Field(default=None, max_length=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS)
    granted: bool | None = None


class ArtifactProduced(AgentEventBase):
    """Execution artifact recorded during the run."""

    event_type: Literal["artifact_produced"] = "artifact_produced"
    name: str
    uri: str
    artifact_type: str | None = None


class BudgetUpdated(AgentEventBase):
    """Resource or token consumption update."""

    event_type: Literal["budget_updated"] = "budget_updated"
    tokens_used: int | None = None
    cost_usd: float | None = None
    context_window_ratio: float | None = None


class AgentFailed(AgentEventBase):
    """Terminal lifecycle failure event."""

    event_type: Literal["agent_failed"] = "agent_failed"
    failure_summary: str = Field(max_length=AGENT_EVENT_MAX_SUMMARY_CHARS)
    failure_kind: str | None = None
    exit_code: int | None = None


class AgentCompleted(AgentEventBase):
    """Terminal lifecycle success/completion event."""

    event_type: Literal["agent_completed"] = "agent_completed"
    final_summary: str = Field(max_length=AGENT_EVENT_MAX_SUMMARY_CHARS)
    exit_code: int | None = None


AgentEvent = Annotated[
    AgentStarted
    | AgentProgress
    | AgentMessage
    | ToolRequested
    | ToolCompleted
    | FileChanged
    | PermissionRequested
    | ArtifactProduced
    | BudgetUpdated
    | AgentFailed
    | AgentCompleted,
    Field(discriminator="event_type"),
]

agent_event_adapter: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)


def assert_event_sequence_monotonic(
    events: Sequence[AgentEvent], *, strictly_consecutive: bool = False
) -> None:
    """Validate that sequence numbers are strictly monotonic and optionally consecutive 1..N."""
    for idx, event in enumerate(events):
        if idx > 0 and event.sequence <= events[idx - 1].sequence:
            msg = (
                f"Non-monotonic sequence at index {idx}: "
                f"{event.sequence} <= {events[idx - 1].sequence}"
            )
            raise ValueError(msg)
        if strictly_consecutive and event.sequence != idx + 1:
            raise ValueError(
                f"Non-consecutive sequence at index {idx}: expected {idx + 1}, got {event.sequence}"
            )
