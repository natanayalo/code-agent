"""Antigravity-native helpers for the existing secondary CLI worker lane."""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Iterator
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

logger = logging.getLogger(__name__)


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


def _json_object_from_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        logger.warning("Failed to inspect Antigravity path", extra={"path": str(path)})
        return False


def _path_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        logger.warning("Failed to inspect Antigravity directory", extra={"path": str(path)})
        return False


def _migrated_mcp_config(settings_path: Path) -> dict[str, Any] | None:
    settings = _json_object_from_file(settings_path)
    servers = settings.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        return None
    migrated: dict[str, Any] = {}
    for name, value in servers.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        server = dict(value)
        if "serverUrl" not in server:
            legacy_url = server.pop("url", None) or server.pop("httpUrl", None)
            if legacy_url is not None:
                server["serverUrl"] = legacy_url
        else:
            server.pop("url", None)
            server.pop("httpUrl", None)
        migrated[name] = server
    return {"mcpServers": migrated} if migrated else None


def _write_json_if_missing(path: Path, payload: dict[str, Any]) -> bool:
    if _path_exists(path):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        logger.warning(
            "Failed to write migrated Antigravity JSON config", extra={"path": str(path)}
        )
        return False
    return True


def _copytree_if_missing(source: Path, target: Path) -> bool:
    if not _path_exists(source) or _path_exists(target):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source, target)
    except OSError:
        logger.warning(
            "Failed to copy migrated Antigravity directory",
            extra={"source": str(source), "target": str(target)},
        )
        return False
    return True


def _copy_file_if_missing(source: Path, target: Path) -> bool:
    if not _path_exists(source) or _path_exists(target):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, target)
    except OSError:
        logger.warning(
            "Failed to copy migrated Antigravity file",
            extra={"source": str(source), "target": str(target)},
        )
        return False
    return True


def _candidate_gemini_homes(
    adapter: AntigravityCliRuntimeAdapter,
    symlink_source: Path | None,
) -> Iterator[Path]:
    if symlink_source is not None:
        yield symlink_source
    env_gemini_home = adapter.env.get("GEMINI_HOME")
    if env_gemini_home:
        yield Path(env_gemini_home)
    try:
        yield Path.home() / ".gemini"
    except Exception:
        pass
    yield Path("/root/.gemini")


def prepare_antigravity_workspace_migration(
    *,
    adapter: AntigravityCliRuntimeAdapter,
    workspace: WorkspaceHandle,
) -> tuple[Path, list[str]]:
    """Prepare Antigravity-compatible Gemini config without mutating host config."""
    agent_home = workspace.workspace_path / ".agent_home"
    gemini_home = agent_home / ".gemini"
    actions: list[str] = []
    symlink_source: Path | None = None

    if gemini_home.is_symlink():
        try:
            symlink_source = gemini_home.resolve(strict=True)
        except OSError:
            symlink_source = None
        gemini_home.unlink()
        actions.append("replaced_symlinked_gemini_home")
    elif gemini_home.exists() and not gemini_home.is_dir():
        gemini_home.unlink()
        actions.append("replaced_file_gemini_home")

    gemini_home.mkdir(parents=True, exist_ok=True)

    for source in _candidate_gemini_homes(adapter, symlink_source):
        source = source.expanduser()
        if not _path_exists(source) or not _path_is_dir(source):
            continue
        if _copy_file_if_missing(source / "GEMINI.md", gemini_home / "GEMINI.md"):
            actions.append("copied_global_gemini_context")
        if _copytree_if_missing(source / "skills", gemini_home / "antigravity-cli" / "skills"):
            actions.append("copied_global_skills")
        mcp_config = _migrated_mcp_config(source / "settings.json")
        if mcp_config:
            did_migrate_global_mcp = _write_json_if_missing(
                gemini_home / "config" / "mcp_config.json",
                mcp_config,
            )
            if did_migrate_global_mcp:
                actions.append("migrated_global_mcp_config")
        break

    legacy_workspace_skills = workspace.repo_path / ".gemini" / "skills"
    if _copytree_if_missing(legacy_workspace_skills, workspace.repo_path / ".agents" / "skills"):
        actions.append("copied_workspace_skills")

    legacy_workspace_settings = workspace.repo_path / ".gemini" / "settings.json"
    workspace_mcp_config = _migrated_mcp_config(legacy_workspace_settings)
    if workspace_mcp_config and _write_json_if_missing(
        workspace.repo_path / ".agents" / "mcp_config.json",
        workspace_mcp_config,
    ):
        actions.append("migrated_workspace_mcp_config")

    if actions:
        logger.info(
            "Prepared Antigravity Gemini migration config",
            extra={"workspace_id": workspace.workspace_id, "actions": actions},
        )
    return gemini_home, actions


def build_antigravity_native_command(
    *,
    adapter: AntigravityCliRuntimeAdapter,
    workspace: WorkspaceHandle,
    request: WorkerRequest,
    prompt: str,
    runtime_settings: CliRuntimeSettings,
    native_sandbox_enabled: bool,
) -> tuple[list[str], Path, dict[str, Any]]:
    """Build `agy -p` command and per-run settings for Antigravity."""
    tool_permission = antigravity_tool_permission(adapter, request)
    agent_home = workspace.workspace_path / ".agent_home"
    gemini_home, migration_actions = prepare_antigravity_workspace_migration(
        adapter=adapter,
        workspace=workspace,
    )
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
        cwd=workspace.repo_path,
    )
    metadata = {
        "provider": "antigravity",
        "tool_permission": tool_permission,
        "artifact_review_policy": adapter.artifact_review_policy,
        "terminal_sandbox_enabled": native_sandbox_enabled,
        "gemini_home": str(gemini_home),
        "migration_actions": migration_actions,
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
