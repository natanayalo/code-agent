"""Domain models for native-agent CLI execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from apps.observability import SPAN_KIND_LLM
from sandbox.redact import SecretRedactor
from workers.base import ArtifactReference
from workers.constants import (
    DEFAULT_CHANGED_FILES_TIMEOUT_SECONDS as DEFAULT_NATIVE_AGENT_CHANGED_FILES_TIMEOUT_SECONDS,
)
from workers.constants import (
    DEFAULT_DIFF_TIMEOUT_SECONDS as DEFAULT_NATIVE_AGENT_DIFF_TIMEOUT_SECONDS,
)
from workers.constants import (
    DEFAULT_WORKER_TIMEOUT_SECONDS as DEFAULT_NATIVE_AGENT_TIMEOUT_SECONDS,
)


@dataclass
class NativeAgentRunResult:
    """Structured output from one native-agent CLI execution."""

    status: Literal["success", "failure", "error"]
    summary: str
    command: str
    exit_code: int | None
    duration_seconds: float
    timed_out: bool
    final_message: str | None = None
    diff_text: str | None = None
    files_changed: list[str] = field(default_factory=list)
    artifacts: list[ArtifactReference] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    json_payload: dict[str, Any] | None = None
    friction_reports: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class NativeAgentRunRequest:
    """Inputs required for one native-agent CLI execution."""

    command: list[str]
    prompt: str
    repo_path: Path
    workspace_path: Path
    timeout_seconds: int = DEFAULT_NATIVE_AGENT_TIMEOUT_SECONDS
    diff_timeout_seconds: int = DEFAULT_NATIVE_AGENT_DIFF_TIMEOUT_SECONDS
    changed_files_timeout_seconds: int = DEFAULT_NATIVE_AGENT_CHANGED_FILES_TIMEOUT_SECONDS
    env: dict[str, str] | None = None
    final_message_path: Path | None = None
    events_path: Path | None = None
    collect_diff: bool = True
    collect_changed_files: bool = True
    task_id: str | None = None
    session_id: str | None = None
    redactor: SecretRedactor | None = None
    response_format: Literal["text", "json"] = "text"
    response_schema: dict[str, Any] | None = None
    span_kind: str = SPAN_KIND_LLM
