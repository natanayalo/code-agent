"""Antigravity CLI adapter for native-agent execution."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, cast, get_args

from workers.adapter_utils import coerce_positive_int
from workers.cli_runtime import CliRuntimeAdapter, CliRuntimeMessage, CliRuntimeStep
from workers.constants import DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS
from workers.subprocess_env import build_antigravity_subprocess_env

ANTIGRAVITY_EXECUTABLE_ENV_VAR: Final[str] = "CODE_AGENT_ANTIGRAVITY_CLI_BIN"
ANTIGRAVITY_MODEL_ENV_VAR: Final[str] = "CODE_AGENT_ANTIGRAVITY_MODEL"
ANTIGRAVITY_TIMEOUT_ENV_VAR: Final[str] = "CODE_AGENT_ANTIGRAVITY_TIMEOUT_SECONDS"
ANTIGRAVITY_AUTH_DIR_ENV_VAR: Final[str] = "CODE_AGENT_ANTIGRAVITY_AUTH_DIR"
ANTIGRAVITY_NATIVE_SANDBOX_ENABLED_ENV_VAR: Final[str] = (
    "CODE_AGENT_ANTIGRAVITY_NATIVE_SANDBOX_ENABLED"
)
ANTIGRAVITY_TOOL_PERMISSION_ENV_VAR: Final[str] = "CODE_AGENT_ANTIGRAVITY_TOOL_PERMISSION"
ANTIGRAVITY_ARTIFACT_REVIEW_POLICY_ENV_VAR: Final[str] = (
    "CODE_AGENT_ANTIGRAVITY_ARTIFACT_REVIEW_POLICY"
)

AntigravityToolPermission = Literal[
    "request-review",
    "proceed-in-sandbox",
    "always-proceed",
    "strict",
]
AntigravityArtifactReviewPolicy = Literal[
    "asks-for-review",
    "agent-decides",
    "always-proceed",
]

DEFAULT_ANTIGRAVITY_EXECUTABLE: Final[str] = "agy"
DEFAULT_ANTIGRAVITY_TOOL_PERMISSION: Final[AntigravityToolPermission] = "proceed-in-sandbox"
DEFAULT_ANTIGRAVITY_ARTIFACT_REVIEW_POLICY: Final[AntigravityArtifactReviewPolicy] = "agent-decides"

VALID_ANTIGRAVITY_TOOL_PERMISSIONS: Final[frozenset[str]] = frozenset(
    str(value) for value in get_args(AntigravityToolPermission)
)
VALID_ANTIGRAVITY_ARTIFACT_REVIEW_POLICIES: Final[frozenset[str]] = frozenset(
    str(value) for value in get_args(AntigravityArtifactReviewPolicy)
)
LEGACY_ARTIFACT_REVIEW_POLICY_ALIASES: Final[dict[str, AntigravityArtifactReviewPolicy]] = {
    "auto": "agent-decides",
    "manual": "asks-for-review",
}


def _normalize_tool_permission(value: str | None) -> AntigravityToolPermission:
    normalized = (value or "").strip().lower() or DEFAULT_ANTIGRAVITY_TOOL_PERMISSION
    if normalized not in VALID_ANTIGRAVITY_TOOL_PERMISSIONS:
        supported = ", ".join(sorted(VALID_ANTIGRAVITY_TOOL_PERMISSIONS))
        raise ValueError(
            f"Invalid Antigravity tool permission: '{value}'. Expected one of: {supported}."
        )
    return cast(AntigravityToolPermission, normalized)


def _normalize_artifact_review_policy(value: str | None) -> AntigravityArtifactReviewPolicy:
    normalized = (value or "").strip().lower() or DEFAULT_ANTIGRAVITY_ARTIFACT_REVIEW_POLICY
    normalized = LEGACY_ARTIFACT_REVIEW_POLICY_ALIASES.get(normalized, normalized)
    if normalized not in VALID_ANTIGRAVITY_ARTIFACT_REVIEW_POLICIES:
        supported = ", ".join(sorted(VALID_ANTIGRAVITY_ARTIFACT_REVIEW_POLICIES))
        raise ValueError(
            f"Invalid Antigravity artifact review policy: '{value}'. Expected one of: {supported}."
        )
    return cast(AntigravityArtifactReviewPolicy, normalized)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def build_antigravity_settings(
    *,
    tool_permission: str,
    artifact_review_policy: str,
    enable_terminal_sandbox: bool,
) -> dict[str, object]:
    """Build the Antigravity settings payload for a non-interactive native run."""
    return {
        "toolPermission": _normalize_tool_permission(tool_permission),
        "artifactReviewPolicy": _normalize_artifact_review_policy(artifact_review_policy),
        "enableTerminalSandbox": enable_terminal_sandbox,
    }


def write_antigravity_settings(
    *,
    agent_home: Path,
    tool_permission: str,
    artifact_review_policy: str,
    enable_terminal_sandbox: bool,
) -> Path:
    """Merge Antigravity non-interactive settings into the workspace agent HOME."""
    settings_dir = agent_home / ".gemini" / "antigravity-cli"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"

    existing: dict[str, object] = {}
    if settings_path.exists():
        try:
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {}
        if isinstance(payload, dict):
            existing = payload

    existing.update(
        build_antigravity_settings(
            tool_permission=tool_permission,
            artifact_review_policy=artifact_review_policy,
            enable_terminal_sandbox=enable_terminal_sandbox,
        )
    )
    settings_path.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return settings_path


class AntigravityCliRuntimeAdapter(CliRuntimeAdapter):
    """Resolve Antigravity CLI settings and command construction for native runs."""

    def __init__(
        self,
        *,
        executable: str = DEFAULT_ANTIGRAVITY_EXECUTABLE,
        model: str | None = None,
        request_timeout_seconds: int = DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS,
        tool_permission: str = DEFAULT_ANTIGRAVITY_TOOL_PERMISSION,
        artifact_review_policy: str = DEFAULT_ANTIGRAVITY_ARTIFACT_REVIEW_POLICY,
        env: Mapping[str, str] | None = None,
    ) -> None:
        resolved_env = os.environ if env is None else env
        self.executable = executable
        self.model = _normalize_optional_text(model)
        self.request_timeout_seconds = coerce_positive_int(
            request_timeout_seconds,
            default=DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS,
        )
        self.tool_permission = _normalize_tool_permission(tool_permission)
        self.artifact_review_policy = _normalize_artifact_review_policy(artifact_review_policy)
        self.env = build_antigravity_subprocess_env(resolved_env)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> AntigravityCliRuntimeAdapter:
        """Build an adapter from Antigravity-specific app environment variables."""
        resolved_env = os.environ if environ is None else environ
        return cls(
            executable=resolved_env.get(
                ANTIGRAVITY_EXECUTABLE_ENV_VAR,
                resolved_env.get("CODE_AGENT_GEMINI_CLI_BIN", DEFAULT_ANTIGRAVITY_EXECUTABLE),
            ),
            model=resolved_env.get(ANTIGRAVITY_MODEL_ENV_VAR)
            or resolved_env.get("CODE_AGENT_GEMINI_MODEL"),
            request_timeout_seconds=coerce_positive_int(
                resolved_env.get(ANTIGRAVITY_TIMEOUT_ENV_VAR)
                or resolved_env.get("CODE_AGENT_GEMINI_TIMEOUT_SECONDS"),
                default=DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS,
            ),
            tool_permission=resolved_env.get(
                ANTIGRAVITY_TOOL_PERMISSION_ENV_VAR,
                DEFAULT_ANTIGRAVITY_TOOL_PERMISSION,
            ),
            artifact_review_policy=resolved_env.get(
                ANTIGRAVITY_ARTIFACT_REVIEW_POLICY_ENV_VAR,
                DEFAULT_ANTIGRAVITY_ARTIFACT_REVIEW_POLICY,
            ),
            env=resolved_env,
        )

    def build_native_command(
        self,
        *,
        prompt: str,
        cwd: Path | None = None,
    ) -> list[str]:
        """Build the documented one-shot Antigravity CLI command."""
        command = [self.executable, "-p", prompt]
        if self.model is not None:
            command.extend(["--model", self.model])
        return command

    def next_step(
        self,
        messages: Sequence[CliRuntimeMessage],
        *,
        system_prompt: str | None = None,
        prompt_override: str | None = None,
        working_directory: Path | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        response_format: Literal["text", "json"] = "text",
        response_schema: dict[str, Any] | None = None,
    ) -> CliRuntimeStep:
        """Antigravity is supported only through native-agent one-shot execution."""
        raise RuntimeError("AntigravityCliRuntimeAdapter supports native_agent mode only.")
