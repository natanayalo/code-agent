"""Domain models for native-agent CLI execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from workers.base import ArtifactReference


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
