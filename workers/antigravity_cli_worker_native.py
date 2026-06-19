"""Antigravity-native helpers for the existing secondary CLI worker lane."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sandbox import WorkspaceHandle
from workers.antigravity_cli_adapter import (
    AntigravityCliRuntimeAdapter,
    write_antigravity_settings,
)
from workers.base import WorkerRequest
from workers.cli_runtime import CliRuntimeSettings

ANTIGRAVITY_READ_ONLY_TOOL_PERMISSION = "strict"


def is_antigravity_native_adapter(adapter: object) -> bool:
    """Return whether the runtime adapter dispatches to the Antigravity CLI."""
    return isinstance(adapter, AntigravityCliRuntimeAdapter)


def antigravity_tool_permission(
    adapter: AntigravityCliRuntimeAdapter,
    request: WorkerRequest,
) -> str:
    """Map read-only requests to strict Antigravity tool permissions."""
    read_only_requested = request.read_only or bool(request.constraints.get("read_only"))
    if read_only_requested:
        return ANTIGRAVITY_READ_ONLY_TOOL_PERMISSION
    return adapter.tool_permission


def build_antigravity_native_command(
    *,
    adapter: AntigravityCliRuntimeAdapter,
    workspace: WorkspaceHandle,
    request: WorkerRequest,
    prompt: str,
    runtime_settings: CliRuntimeSettings,
    native_sandbox_enabled: bool,
) -> tuple[list[str], Path, dict[str, Any]]:
    """Build `agy --print` command and per-run settings for Antigravity."""
    tool_permission = antigravity_tool_permission(adapter, request)
    agent_home = workspace.workspace_path / ".agent_home"
    settings_path = write_antigravity_settings(
        agent_home=agent_home,
        tool_permission=tool_permission,
        artifact_review_policy=adapter.artifact_review_policy,
        enable_terminal_sandbox=native_sandbox_enabled,
    )
    log_file = workspace.workspace_path / ".code-agent" / "antigravity-native.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    command = adapter.build_native_command(
        prompt=prompt,
        print_timeout_seconds=runtime_settings.worker_timeout_seconds,
        log_file=log_file,
    )
    metadata = {
        "provider": "antigravity",
        "tool_permission": tool_permission,
        "artifact_review_policy": adapter.artifact_review_policy,
        "terminal_sandbox_enabled": native_sandbox_enabled,
        "settings_path": str(settings_path),
        "log_file": str(log_file),
    }
    return command, log_file, metadata


def antigravity_permission_denied(summary: str) -> bool:
    """Return whether an Antigravity failure summary indicates tool permission denial."""
    summary_l = summary.lower()
    return any(
        marker in summary_l
        for marker in (
            "permission denied",
            "permission prompt",
            "permission required",
            "requires user confirmation",
            "tool permission",
        )
    )
