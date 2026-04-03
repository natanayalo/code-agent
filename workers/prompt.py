"""Structured system prompt construction helpers for coding workers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from workers.base import WorkerRequest

DEFAULT_AVAILABLE_TOOLS = ("execute_bash",)
DEFAULT_REPO_LISTING_MAX_DEPTH = 2
DEFAULT_REPO_LISTING_MAX_ENTRIES = 40
DEFAULT_AGENTS_MAX_CHARACTERS = 6000
_SKIPPED_PATH_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}


def build_role_description_section() -> str:
    """Describe the worker's job and guardrails."""
    return "\n".join(
        [
            "## Role",
            "You are the coding execution worker for code-agent.",
            "Work inside the checked-out repository, make the smallest safe changes that satisfy",
            "the task, and keep your reasoning grounded in the files and command output available",
            "inside the workspace.",
            "Do not mutate orchestrator state directly, write to memory directly, bypass sandbox",
            "rules, or invent results you did not verify.",
        ]
    )


def build_available_tools_section(
    available_tools: Sequence[str] | None = None,
) -> str:
    """Render the configured worker tool surface."""
    normalized_tools = tuple(
        dict.fromkeys(
            tool.strip()
            for tool in (available_tools or DEFAULT_AVAILABLE_TOOLS)
            if tool is not None and tool.strip()
        )
    )
    tool_lines = [f"- `{tool}`" for tool in normalized_tools] or ["- No tools configured."]
    return "\n".join(["## Available Tools", *tool_lines])


def read_workspace_agents_guidance(
    workspace_path: Path,
    *,
    max_characters: int = DEFAULT_AGENTS_MAX_CHARACTERS,
) -> str | None:
    """Return bounded AGENTS.md guidance from the workspace root when present."""
    agents_path = workspace_path / "AGENTS.md"
    if not agents_path.is_file():
        return None

    contents = agents_path.read_text(encoding="utf-8", errors="replace").strip()
    if len(contents) <= max_characters:
        return contents
    truncated = contents[:max_characters].rstrip()
    return f"{truncated}\n... (truncated)"


def build_workspace_directory_listing(
    workspace_path: Path,
    *,
    max_depth: int = DEFAULT_REPO_LISTING_MAX_DEPTH,
    max_entries: int = DEFAULT_REPO_LISTING_MAX_ENTRIES,
) -> str:
    """Build a deterministic bounded directory listing for prompt context."""
    root = workspace_path
    if not root.exists():
        return "<workspace path does not exist>"
    if not root.is_dir():
        return "<workspace path is not a directory>"

    entries: list[str] = []
    truncated = False

    def visit(path: Path, *, depth: int) -> None:
        nonlocal truncated
        if truncated or depth >= max_depth:
            return

        try:
            children = sorted(
                (child for child in path.iterdir() if child.name not in _SKIPPED_PATH_NAMES),
                key=lambda child: (not child.is_dir(), child.name.lower()),
            )
        except OSError:
            return
        for child in children:
            if len(entries) >= max_entries:
                truncated = True
                return
            relative_path = child.relative_to(root).as_posix()
            entries.append(f"{relative_path}/" if child.is_dir() else relative_path)
            if child.is_dir():
                visit(child, depth=depth + 1)

    visit(root, depth=0)

    if not entries:
        return "<workspace is empty>"
    if truncated:
        entries.append("... (truncated)")
    return "\n".join(entries)


def build_repo_context_section(workspace_path: Path) -> str:
    """Render repo-level prompt context from the workspace."""
    lines = [
        "## Repo Context",
        "Directory listing:",
        "```text",
        build_workspace_directory_listing(workspace_path),
        "```",
    ]
    agents_guidance = read_workspace_agents_guidance(workspace_path)
    if agents_guidance is not None:
        lines.extend(
            [
                "AGENTS.md guidance:",
                "```text",
                agents_guidance,
                "```",
            ]
        )
    return "\n".join(lines)


def _json_safe(value: Any) -> Any:
    """Normalize prompt context data into JSON-safe values."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(
            (_json_safe(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    return str(value)


def _render_json_block(value: Any) -> str:
    """Pretty-print a JSON-safe object for prompt inclusion."""
    return json.dumps(_json_safe(value), indent=2, sort_keys=True)


def _mask_repo_url(repo_url: str | None) -> str | None:
    """Hide inline credentials before rendering repository context in prompts."""
    if repo_url is None:
        return None
    scheme, marker, remainder = repo_url.partition("://")
    if not marker or "@" not in remainder:
        return repo_url
    credentials, _, host = remainder.rpartition("@")
    if not credentials:
        return repo_url
    return f"{scheme}{marker}***@{host}"


def build_task_context_section(request: WorkerRequest) -> str:
    """Render task-specific prompt context from the normalized worker request."""
    lines = [
        "## Task Context",
        f"Task text: {request.task_text}",
        f"Session ID: {request.session_id or 'unknown'}",
        f"Repository URL: {_mask_repo_url(request.repo_url) or 'not provided'}",
        f"Branch: {request.branch or 'default'}",
    ]
    if request.memory_context:
        lines.extend(
            [
                "Memory context:",
                "```json",
                _render_json_block(request.memory_context),
                "```",
            ]
        )
    if request.constraints:
        lines.extend(
            [
                "Constraints:",
                "```json",
                _render_json_block(request.constraints),
                "```",
            ]
        )
    if request.budget:
        lines.extend(
            [
                "Budget:",
                "```json",
                _render_json_block(request.budget),
                "```",
            ]
        )
    return "\n".join(lines)


def build_workflow_instructions_section() -> str:
    """Describe the expected worker execution workflow."""
    return "\n".join(
        [
            "## Workflow Instructions",
            "- Inspect relevant files before making changes.",
            "- Prefer minimal, reviewable edits over broad rewrites.",
            "- Use the available tools with focused commands and targeted reads; avoid dumping "
            "large files or verbose output, and narrow long or truncated results with `rg`, "
            "`sed -n`, `head`, `tail`, or focused test selectors before continuing.",
            "- If a command fails, inspect the current files, paths, and assumptions before "
            "retrying; do not blindly repeat the same command.",
            "- Base the next step on command exit codes and the relevant output you actually "
            "observed.",
            "- Surface blockers or missing prerequisites explicitly instead of guessing.",
            "- End with a concise summary of changes, verification, and any follow-up needed.",
        ]
    )


def build_system_prompt(
    request: WorkerRequest,
    workspace_path: Path,
    *,
    available_tools: Sequence[str] | None = None,
) -> str:
    """Assemble the structured system prompt for a coding worker run."""
    sections = [
        build_role_description_section(),
        build_available_tools_section(available_tools),
        build_repo_context_section(workspace_path),
        build_task_context_section(request),
        build_workflow_instructions_section(),
    ]
    return "\n\n".join(section for section in sections if section.strip())
