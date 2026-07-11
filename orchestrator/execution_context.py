"""Small dataclasses shared across execution-service helper modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    decomposed_plan: dict[str, Any] | None = None
    node_outcomes: list[dict[str, Any]] = field(default_factory=list)
