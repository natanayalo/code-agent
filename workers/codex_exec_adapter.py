"""Concrete Codex CLI adapter for the shared multi-turn runtime."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from workers.cli_runtime import CliRuntimeAdapter, CliRuntimeMessage, CliRuntimeStep

DEFAULT_CODEX_EXECUTABLE: Final[str] = "codex"
DEFAULT_CODEX_SANDBOX_MODE: Final[str] = "read-only"
DEFAULT_CODEX_REQUEST_TIMEOUT_SECONDS: Final[int] = 120
_DETAIL_PREVIEW_CHARACTERS: Final[int] = 1200

CODEX_EXECUTABLE_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_CLI_BIN"
CODEX_MODEL_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_MODEL"
CODEX_PROFILE_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_PROFILE"
CODEX_TIMEOUT_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_TIMEOUT_SECONDS"
CODEX_SANDBOX_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_SANDBOX"


def _coerce_positive_int(value: object, *, default: int) -> int:
    """Parse a positive integer override or fall back to the default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value > 0 else default
    if isinstance(value, float):
        try:
            parsed = int(value)
        except (OverflowError, ValueError):
            return default
        return parsed if parsed > 0 else default
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            parsed = int(float(stripped))
        except (OverflowError, ValueError):
            return default
        return parsed if parsed > 0 else default
    return default


def _truncate_detail(text: str, *, max_characters: int = _DETAIL_PREVIEW_CHARACTERS) -> str:
    """Render bounded stderr/stdout details for adapter failures."""
    stripped = text.strip()
    if not stripped:
        return "<empty>"
    if len(stripped) <= max_characters:
        return stripped
    return f"[truncated]...{stripped[-max_characters:].lstrip()}"


def _message_heading(message: CliRuntimeMessage, *, index: int) -> str:
    """Render a compact transcript heading for one runtime message."""
    if message.role == "tool":
        return f"### Message {index} ({message.role}:{message.tool_name})"
    return f"### Message {index} ({message.role})"


def _build_adapter_prompt(messages: Sequence[CliRuntimeMessage]) -> str:
    """Build the single-shot prompt sent to `codex exec` for one runtime turn."""
    lines = [
        "You are the Codex runtime adapter for a bounded coding worker.",
        (
            "Read the transcript below and return exactly one JSON object "
            "matching the provided schema."
        ),
        "Choose one of two actions:",
        (
            '- {"kind":"tool_call","tool_name":"<registered tool name>",'
            '"tool_input":"<tool input string>","final_output":null}'
        ),
        (
            '- {"kind":"final","final_output":"<final summary for the user>",'
            '"tool_name":null,"tool_input":null}'
        ),
        "Rules:",
        "- Use only tool names listed in the system prompt's Available Tools section.",
        "- For `execute_bash`, return one focused shell command as the tool_input string.",
        (
            "- For `execute_git`, return the tool_input as a compact JSON object encoded "
            'as a string, for example {"operation":"status","porcelain":true}.'
        ),
        (
            "- For `execute_github`, return the tool_input as a compact JSON object encoded "
            "as a string, for example "
            '{"operation":"pr_comment","repository_full_name":"owner/repo",'
            '"pr_number":1,"comment_body":"Looks good."}.'
        ),
        (
            "- For `execute_browser`, return the tool_input as a compact JSON object encoded "
            "as a string, for example "
            '{"operation":"search","query":"langgraph","limit":3}.'
        ),
        "- If the transcript already contains enough information to finish, return `final`.",
        "- If the latest tool result failed, adapt to that failure instead of repeating blindly.",
        "- Do not wrap the JSON in Markdown or add any extra prose.",
        "",
        "## Runtime Transcript",
    ]
    for index, message in enumerate(messages, start=1):
        lines.extend((_message_heading(message, index=index), message.content, ""))
    return "\n".join(lines).rstrip()


def _codex_output_schema() -> dict[str, object]:
    """Return the strict JSON schema shape accepted by `codex exec --output-schema`."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["tool_call", "final"],
            },
            "tool_name": {
                "type": ["string", "null"],
            },
            "tool_input": {
                "type": ["string", "null"],
            },
            "final_output": {
                "type": ["string", "null"],
            },
        },
        "required": ["kind", "tool_name", "tool_input", "final_output"],
    }


class CodexExecCliRuntimeAdapter(CliRuntimeAdapter):
    """Resolve runtime turns by shelling out to `codex exec`."""

    def __init__(
        self,
        *,
        executable: str = DEFAULT_CODEX_EXECUTABLE,
        model: str | None = None,
        profile: str | None = None,
        sandbox_mode: str = DEFAULT_CODEX_SANDBOX_MODE,
        request_timeout_seconds: int = DEFAULT_CODEX_REQUEST_TIMEOUT_SECONDS,
        working_directory: str | Path | None = None,
        config_overrides: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.executable = executable
        self.model = model.strip() if model is not None and model.strip() else None
        self.profile = profile.strip() if profile is not None and profile.strip() else None
        self.sandbox_mode = sandbox_mode.strip() or DEFAULT_CODEX_SANDBOX_MODE
        self.request_timeout_seconds = _coerce_positive_int(
            request_timeout_seconds, default=DEFAULT_CODEX_REQUEST_TIMEOUT_SECONDS
        )
        self.working_directory = Path(working_directory or tempfile.gettempdir()).expanduser()
        self.config_overrides = tuple(
            override.strip()
            for override in config_overrides
            if isinstance(override, str) and override.strip()
        )
        self.env = dict(env) if env is not None else None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> CodexExecCliRuntimeAdapter:
        """Build an adapter from the app's environment variables."""
        resolved_env = os.environ if environ is None else environ
        return cls(
            executable=resolved_env.get(CODEX_EXECUTABLE_ENV_VAR, DEFAULT_CODEX_EXECUTABLE),
            model=resolved_env.get(CODEX_MODEL_ENV_VAR),
            profile=resolved_env.get(CODEX_PROFILE_ENV_VAR),
            sandbox_mode=resolved_env.get(CODEX_SANDBOX_ENV_VAR, DEFAULT_CODEX_SANDBOX_MODE),
            request_timeout_seconds=_coerce_positive_int(
                resolved_env.get(CODEX_TIMEOUT_ENV_VAR),
                default=DEFAULT_CODEX_REQUEST_TIMEOUT_SECONDS,
            ),
            env=resolved_env,
        )

    def _build_command(
        self,
        *,
        output_schema_path: Path,
        output_message_path: Path,
        working_directory: Path | None = None,
    ) -> list[str]:
        """Build the `codex exec` argv for one adapter turn."""
        command = [
            self.executable,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            self.sandbox_mode,
            "--color",
            "never",
            "--output-schema",
            str(output_schema_path),
            "--output-last-message",
            str(output_message_path),
            "--ephemeral",
            "-C",
            str(working_directory or self.working_directory),
        ]
        if self.model is not None:
            command.extend(["--model", self.model])
        if self.profile is not None:
            command.extend(["--profile", self.profile])
        for override in self.config_overrides:
            command.extend(["-c", override])
        command.append("-")
        return command

    def next_step(
        self,
        messages: Sequence[CliRuntimeMessage],
        *,
        working_directory: Path | None = None,
    ) -> CliRuntimeStep:
        """Ask the Codex CLI for the next runtime step."""
        prompt = _build_adapter_prompt(messages)
        with tempfile.TemporaryDirectory(prefix="code-agent-codex-step-") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            schema_path = temp_dir / "cli_runtime_step.schema.json"
            output_message_path = temp_dir / "last_message.json"
            schema_path.write_text(
                json.dumps(_codex_output_schema(), indent=2, sort_keys=True),
                encoding="utf-8",
            )

            try:
                completed = subprocess.run(
                    self._build_command(
                        output_schema_path=schema_path,
                        output_message_path=output_message_path,
                        working_directory=working_directory,
                    ),
                    input=prompt,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=self.request_timeout_seconds,
                    env=self.env,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "Codex CLI adapter timed out after "
                    f"{self.request_timeout_seconds}s while selecting the next runtime step."
                ) from exc
            except OSError as exc:
                raise RuntimeError(
                    f"Codex CLI adapter could not start `{self.executable}`: {exc}"
                ) from exc

            if completed.returncode != 0:
                raise RuntimeError(
                    "Codex CLI adapter failed with exit code "
                    f"{completed.returncode}. stderr: {_truncate_detail(completed.stderr)} "
                    f"stdout: {_truncate_detail(completed.stdout)}"
                )

            if not output_message_path.exists():
                raise RuntimeError(
                    "Codex CLI adapter completed without writing the final message file. "
                    f"stdout: {_truncate_detail(completed.stdout)} "
                    f"stderr: {_truncate_detail(completed.stderr)}"
                )

            raw_output = output_message_path.read_text(encoding="utf-8").strip()
            if not raw_output:
                raise RuntimeError(
                    "Codex CLI adapter wrote an empty final message file. "
                    f"stdout: {_truncate_detail(completed.stdout)} "
                    f"stderr: {_truncate_detail(completed.stderr)}"
                )

            try:
                return CliRuntimeStep.model_validate_json(raw_output)
            except (
                Exception
            ) as exc:  # pragma: no cover - exercised via tests with specific payloads.
                raise RuntimeError(
                    "Codex CLI adapter returned a final message that did not match "
                    f"CliRuntimeStep: "
                    f"{_truncate_detail(raw_output)}"
                ) from exc
