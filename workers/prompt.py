"""Structured system prompt construction helpers for coding workers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final, Literal, cast, get_args

from db.enums import WorkerRuntimeMode
from tools import McpToolClient, ToolRegistry
from workers.base import WorkerRequest
from workers.markdown import markdown_fence_for_content
from workers.prompt_tools import (
    build_available_tools_section,
)
from workers.prompt_workspace import (
    _build_repo_context_section_with_guidance,
    _truncate_to_budget,
)

DEFAULT_REPO_LISTING_MAX_DEPTH = 2
DEFAULT_REPO_LISTING_MAX_ENTRIES = 40
DEFAULT_AGENTS_MAX_CHARACTERS = 5000
DEFAULT_AGENTS_ASSET_READ_MAX_CHARACTERS = 8192
_SECTION_SEPARATOR_OVERHEAD_BUFFER = 32
_GUIDANCE_OVERHEAD_BUFFER = 100
_REVIEW_ROLE_SECTION = "\n".join(
    [
        "## Review Role",
        "You are the review worker for code-agent.",
        "Focus on high-confidence, actionable findings grounded in the workspace state.",
        "You have full tool access; use `git diff`, `read_file`, and other tools to inspect ",
        "the changes and their impact before finalizing your review.",
        "Prefer precision over recall and skip style-only or speculative comments.",
        "Do not propose broad rewrites when a focused finding is sufficient.",
    ]
)
_REVIEW_SCHEMA_PAYLOAD = {
    "reviewer_kind": "string",
    "summary": "string",
    "confidence": 0.0,
    "outcome": "no_findings|findings",
    "findings": [
        {
            "severity": "low|medium|high|critical",
            "category": "string",
            "confidence": 0.0,
            "file_path": "string",
            "line_start": 1,
            "line_end": 1,
            "title": "string",
            "why_it_matters": "string",
            "evidence": "string|null",
            "suggested_fix": "string|null",
        }
    ],
}
_REVIEW_OUTPUT_CONTRACT_TEMPLATE = "\n".join(
    [
        "## Output Contract",
        "Return exactly one JSON object. Your response MUST NOT contain any markdown ",
        "fences or extra prose outside of the JSON payload.",
        "Schema:",
        "```json",
        "{schema_json}",
        "```",
        "Rules:",
        "- Use outcome `no_findings` with an empty `findings` list when nothing actionable exists.",
        "- Use outcome `findings` only when at least one concrete actionable finding exists.",
        "- Base your findings on the actual file contents and diff observed via tools.",
    ]
)
_TRUNCATED_MARKER = "\n... (truncated)"
_AGENTS_ASSET_DIRECTORIES = ("skills", "workflows", "rules")
_BUILD_CONTEXT_ITEM_LIMIT = 8
_BUILD_CONTEXT_VALUE_MAX_CHARACTERS = 220
_SKIPPED_PATH_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}


def _fenced_text_block_lines(label: str, content: str, *, fence: str | None = None) -> list[str]:
    """Render a text block with a collision-safe markdown fence."""
    actual_fence = fence if fence is not None else markdown_fence_for_content(content)
    return [label, f"{actual_fence}text", content, actual_fence]


def _fenced_text_block_overhead(label: str, content: str, *, fence: str | None = None) -> int:
    """Return wrapper-character overhead for one fenced guidance block."""
    actual_fence = fence if fence is not None else markdown_fence_for_content(content)
    # label + opening fence + closing fence + three line separators
    return len(label) + len(f"{actual_fence}text") + len(actual_fence) + 3


def build_role_description_section(request: WorkerRequest) -> str:
    """Describe the worker's job and guardrails."""
    is_read_only = request.read_only or bool(request.constraints.get("read_only"))
    permissions = "read" if is_read_only else "read/write"
    action = "analyze files" if is_read_only else "make smallest safe changes"
    mutation_guard = " Do not modify files." if is_read_only else ""

    role_lines = [
        "## Role",
        f"You are the coding execution worker with {permissions} permissions. "
        f"Work inside the repository, {action}.{mutation_guard}"
        "Your first action MUST be to read `AGENTS.md` to understand repository policy. "
        "Keep reasoning grounded in observed state. Do not bypass sandbox rules.",
    ]
    if request.runtime_mode == WorkerRuntimeMode.NATIVE_AGENT:
        role_lines.append(
            "You have full autonomy as a native agent. Use your internal tools, reasoning, "
            "and execution loop to achieve the task objective."
        )
    return "\n".join(role_lines)


def _compact_json_summary(value: Any) -> str:
    """Render JSON data into one bounded line for prompt summaries."""
    serialized = json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"))
    return _truncate_to_budget(serialized, max_characters=_BUILD_CONTEXT_VALUE_MAX_CHARACTERS)


def build_build_test_section(
    workspace_path: Path,
    *,
    max_characters: int = DEFAULT_AGENTS_MAX_CHARACTERS,
) -> str | None:
    """Render a compact summary of build/test metadata."""
    if max_characters <= 0:
        return None
    parts = []
    if (workspace_path / "pyproject.toml").exists():
        parts.append("- Build/Test config found in `pyproject.toml`.")
    if (workspace_path / "Dockerfile").exists():
        parts.append("- Deployment config found in `Dockerfile`.")
    if (workspace_path / ".github/workflows").is_dir():
        parts.append("- CI workflows found in `.github/workflows/`.")
    if not parts:
        return None
    section = "\n".join(["## Build & Test", *parts])
    return _truncate_to_budget(section, max_characters=max_characters)


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
        f"Goal: {request.task_text}",
    ]

    spec = request.task_spec or {}
    if spec:
        for field in ("assumptions", "acceptance_criteria", "non_goals", "verification_commands"):
            values = spec.get(field)
            if isinstance(values, list) and values:
                lines.append(f"{field.replace('_', ' ').capitalize()}:")
                lines.extend([f"- {v}" for v in values])

    if request.constraints:
        # Only include high-level constraints, omit implementation details like lint commands
        visible_constraints = {
            k: v
            for k, v in request.constraints.items()
            if k in {"risk_level", "priority", "read_only", "requires_approval"}
        }
        if visible_constraints:
            lines.extend(
                ["Constraints:", "```json", _render_json_block(visible_constraints), "```"]
            )

    return "\n".join(lines)


def build_workflow_instructions_section(request: WorkerRequest) -> str:
    """Describe the expected worker execution workflow."""
    is_read_only = bool(request.constraints.get("read_only"))
    lines = [
        "## Workflow Instructions",
        "- Inspect files before making decisions.",
    ]
    if not is_read_only:
        lines.append("- Prefer minimal edits over broad rewrites.")

    lines.extend(
        [
            "- The persistent shell already starts in the checked-out workspace repository; "
            "treat `repo_url` as the clone source, not as a filesystem path to `cd` into.",
            "- Use tools with focused commands; avoid dumping large files.",
            "- Base steps on observed output and exit codes.",
            "- Surface blockers explicitly instead of guessing.",
            "- End with a concise summary.",
        ]
    )
    return "\n".join(lines)


ScoutMode = Literal["repo", "research", "deep"]
SCOUT_MODES: Final[tuple[ScoutMode, ...]] = get_args(ScoutMode)
SCOUT_JSON_CONTRACT: Final = (
    "Return exactly one JSON object and no markdown, no code fence, and no prose outside JSON. "
    'The object must be `{ "proposals": [...] }`; each proposal item must include '
    "`title`, `description`, `value`, `effort`, `risk`, `layer_impact`, "
    "`validation_path`, `hitl_need`, `evidence`, and `implementation_slice`."
)


def _scout_max_proposals_from_constraints(constraints: dict[str, Any]) -> int:
    raw_max_proposals = constraints.get("max_proposals")
    try:
        max_proposals = int(raw_max_proposals) if raw_max_proposals is not None else 3
        if max_proposals <= 0:
            return 3
        return max_proposals
    except (ValueError, TypeError):
        return 3


def build_scout_overlay_section(request: WorkerRequest) -> str:
    """Describe scout mode instructions and proposal-oriented output rules."""
    task_spec_type = (
        request.task_spec.get("task_type") if isinstance(request.task_spec, dict) else None
    )
    if request.constraints.get("task_type") != "scout" and task_spec_type != "scout":
        return ""

    raw_mode = request.constraints.get("scout_mode")
    scout_mode = cast(ScoutMode, raw_mode) if raw_mode in SCOUT_MODES else cast(ScoutMode, "repo")
    raw_focus = request.constraints.get("scout_focus")
    scout_focus = raw_focus.strip() or None if isinstance(raw_focus, str) else None

    raw_depth = request.constraints.get("scout_depth")
    scout_depth = raw_depth.strip() or None if isinstance(raw_depth, str) else None

    max_proposals = _scout_max_proposals_from_constraints(request.constraints)

    lines = [
        "## Scout Mode Guardrails",
        "You operate in a strictly read-only mode. Do not attempt to merge, "
        "deploy, or modify the main codebase.",
        f"Your final output must be up to {max_proposals} structured proposal(s) "
        "for the Idea Inbox.",
        SCOUT_JSON_CONTRACT,
        "Use these exact lowercase enum values:",
        "- value: `low`, `medium`, or `high`",
        "- effort: `small`, `medium`, or `large`",
        "- risk: `low`, `medium`, or `high`",
        "- layer_impact: `orchestrator`, `worker`, `sandbox`, `api`, `dashboard`, or `other`",
        "- hitl_need: `required`, `optional`, or `none`",
        "Evidence must be a non-empty array of concrete file paths, line numbers, "
        "observed command output, or external references.",
    ]

    lines.append(f"\nMode: `{scout_mode}`")
    if scout_depth:
        lines.append(f"Depth: `{scout_depth}`")
    if scout_focus:
        lines.append(f"Focus: {scout_focus}")

    lines.append("\nMode Instructions:")
    if scout_mode == "repo":
        lines.append(
            "- Focus on inspecting the local repository to identify technical debt, "
            "refactoring opportunities, or bugs."
        )
    elif scout_mode == "research":
        lines.append(
            "- Focus on researching specific topics or external references to propose "
            "architectural improvements."
        )
        if scout_focus:
            lines.append(f"  Pay close attention to the requested focus area: {scout_focus}")
        lines.append(
            "- Source Policy: use available/local evidence first, cite external references "
            "only if tools/network policy permits, and explicitly state when external "
            "research could not be performed."
        )
    elif scout_mode == "deep":
        lines.append(
            "- Use a repo-first, then targeted-research structure when available; "
            "do not exceed the task budget."
        )
        lines.append(
            "- Source Policy: use available/local evidence first, cite external references "
            "only if tools/network policy permits, and explicitly state when external "
            "research could not be performed."
        )

    return "\n".join(lines)


def build_system_prompt(
    request: WorkerRequest,
    workspace_path: Path,
    *,
    tool_registry: ToolRegistry | None = None,
    tool_client: McpToolClient | None = None,
) -> str:
    """Assemble the structured system prompt for a coding worker run."""
    from tools.policy import (
        ToolPermissionLevel,
        granted_permission_from_constraints,
        permission_allows,
    )

    is_native = request.runtime_mode == WorkerRuntimeMode.NATIVE_AGENT
    permission_level = granted_permission_from_constraints(request.constraints)
    if request.read_only or bool(request.constraints.get("read_only")):
        # Ensure we don't upgrade a more restrictive permission (unlikely but safe)
        from tools.policy import permission_rank

        if permission_rank(permission_level) > permission_rank(ToolPermissionLevel.READ_ONLY):
            permission_level = ToolPermissionLevel.READ_ONLY

    # Resolve the set of tools that should be visible in the prompt
    allowed_tool_names: set[str] | None = None
    if tool_registry is not None:
        all_tools = tool_registry.list_tools()
        # Primary filter: only show tools that are permitted at the current level
        permitted_tools = [
            t for t in all_tools if permission_allows(permission_level, t.required_permission)
        ]
        # Secondary filter: if request explicitly asked for a subset, use that
        if request.tools is not None:
            requested_names = set(request.tools)
            permitted_tools = [t for t in permitted_tools if t.name in requested_names]

        allowed_tool_names = {t.name for t in permitted_tools}

    sections = [
        build_role_description_section(request),
        build_scout_overlay_section(request),
        build_available_tools_section(tool_registry, tool_client, allowed_tool_names)
        if not is_native
        else "",
        _build_repo_context_section_with_guidance(
            workspace_path,
            omit_dir_listing=is_native,
        ),
        build_build_test_section(workspace_path) or "",
        build_task_context_section(request),
        build_workflow_instructions_section(request),
    ]
    return "\n\n".join(section for section in sections if section.strip())
