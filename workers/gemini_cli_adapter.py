"""Concrete Gemini CLI adapter for the shared multi-turn runtime."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from workers.cli_runtime import CliRuntimeAdapter, CliRuntimeMessage, CliRuntimeStep
from workers.prompt import build_runtime_adapter_tool_guidance_lines
from workers.subprocess_env import build_gemini_subprocess_env

DEFAULT_GEMINI_EXECUTABLE: Final[str] = "gemini"
DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS: Final[int] = 120
_DETAIL_PREVIEW_CHARACTERS: Final[int] = 1200

GEMINI_EXECUTABLE_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_CLI_BIN"
GEMINI_MODEL_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_MODEL"
GEMINI_TIMEOUT_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_TIMEOUT_SECONDS"

logger = logging.getLogger(__name__)


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


def _build_adapter_prompt(
    messages: Sequence[CliRuntimeMessage],
    *,
    system_prompt: str | None = None,
) -> str:
    """Build the prompt sent to the Gemini CLI for one runtime turn."""
    tool_guidance_lines = build_runtime_adapter_tool_guidance_lines(system_prompt=system_prompt)
    lines = [
        "You are the Gemini runtime adapter for a bounded coding worker.",
        (
            "Read the transcript below and return exactly one JSON object "
            "with no surrounding text, no markdown fences, and no explanation."
        ),
        "Choose one of two actions:",
        CliRuntimeStep(
            kind="tool_call",
            tool_name="<registered tool name>",
            tool_input=(
                '<tool input as a single escaped string, e.g. "ls -la" or '
                '"{\\"op\\":\\"status\\"}" >'
            ),
            final_output=None,
        ).model_dump_json(),
        CliRuntimeStep(
            kind="final",
            final_output="<final summary for the user>",
            tool_name=None,
            tool_input=None,
        ).model_dump_json(),
        "Rules:",
        "- `kind` must be EXACTLY 'tool_call' or 'final'. NEVER use other values like 'tool_code'.",
        "- Use only tool names listed in the system prompt's Available Tools section.",
        "- `tool_input` MUST be a string. If the tool expects JSON, you must encode "
        "that JSON as a string inside the tool_input field.",
        *tool_guidance_lines,
        "- If the transcript already contains enough information to finish, return `final`.",
        "- If the latest tool result failed, adapt to that failure instead of "
        "repeating blindly.",
        "- Return ONLY a raw JSON object. No markdown fences, no extra explanation.",
    ]
    if system_prompt is not None and system_prompt.strip():
        lines.extend(
            [
                "",
                "## Worker System Prompt",
                system_prompt.strip(),
            ]
        )
    lines.extend(
        [
            "",
            "## Runtime Transcript",
        ]
    )
    for index, message in enumerate(messages, start=1):
        lines.extend((_message_heading(message, index=index), message.content, ""))
    return "\n".join(lines).rstrip()


def _extract_json(text: str) -> str:
    """Extract the first valid JSON object from a Gemini CLI response.

    Uses brace-counting with string awareness to find balanced ``{...}``
    candidates, then validates each with ``json.loads`` before returning it.
    This handles nested objects, prose with embedded non-JSON braces (e.g.
    ``{a, b}``), trailing prose, and markdown-fenced responses correctly.
    """
    stripped = text.strip()
    search_from = 0
    while True:
        start = stripped.find("{", search_from)
        if start == -1:
            break
        depth = 0
        in_string = False
        escape_next = False
        end = -1
        for i, ch in enumerate(stripped[start:], start=start):
            if escape_next:
                escape_next = False
                continue
            if in_string:
                if ch == "\\":
                    escape_next = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            break
        candidate = stripped[start : end + 1]
        try:
            json.loads(candidate)
            return candidate
        except ValueError:
            search_from = end + 1
    raise RuntimeError(f"No JSON object found in Gemini CLI response: {_truncate_detail(stripped)}")


def _coerce_step_payload(parsed: object) -> dict[str, object] | None:
    """Normalize near-miss adapter payloads into CliRuntimeStep-compatible JSON."""
    if not isinstance(parsed, dict):
        return None
    kind = parsed.get("kind")
    if kind not in {"tool_call", "final"}:
        return None

    payload: dict[str, object] = dict(parsed)
    payload.setdefault("tool_name", None)
    payload.setdefault("tool_input", None)
    payload.setdefault("final_output", None)

    if kind == "tool_call" and isinstance(payload.get("tool_input"), dict):
        payload["tool_input"] = json.dumps(payload["tool_input"])
    return payload


class GeminiCliRuntimeAdapter(CliRuntimeAdapter):
    """Resolve runtime turns by shelling out to the Gemini CLI.

    The Gemini CLI is invoked with the transcript prompt on stdin. The adapter
    extracts a ``CliRuntimeStep``-shaped JSON object from the text response.
    """

    def __init__(
        self,
        *,
        executable: str = DEFAULT_GEMINI_EXECUTABLE,
        model: str | None = None,
        request_timeout_seconds: int = DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS,
        working_directory: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        resolved_env = os.environ if env is None else env
        self.executable = executable
        self.model = model.strip() if model is not None and model.strip() else None
        self.request_timeout_seconds = _coerce_positive_int(
            request_timeout_seconds, default=DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS
        )
        self.working_directory = Path(working_directory or tempfile.gettempdir()).expanduser()
        self.env = build_gemini_subprocess_env(resolved_env)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> GeminiCliRuntimeAdapter:
        """Build an adapter from the app's environment variables."""
        resolved_env = os.environ if environ is None else environ
        return cls(
            executable=resolved_env.get(GEMINI_EXECUTABLE_ENV_VAR, DEFAULT_GEMINI_EXECUTABLE),
            model=resolved_env.get(GEMINI_MODEL_ENV_VAR),
            request_timeout_seconds=_coerce_positive_int(
                resolved_env.get(GEMINI_TIMEOUT_ENV_VAR),
                default=DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS,
            ),
            env=resolved_env,
        )

    def _build_command(self) -> list[str]:
        """Build the gemini CLI argv for one adapter turn."""
        command = [self.executable, "chat"]
        if self.model is not None:
            command.extend(["--model", self.model])
        command.extend(["-o", "json", "--accept-raw-output-risk", "--raw-output"])
        return command

    def next_step(
        self,
        messages: Sequence[CliRuntimeMessage],
        *,
        system_prompt: str | None = None,
        prompt_override: str | None = None,
        working_directory: Path | None = None,  # noqa: ARG002 — context only, not used by CLI
    ) -> CliRuntimeStep:
        """Ask the Gemini CLI for the next runtime step."""
        override_prompt = (
            prompt_override.strip() if prompt_override and prompt_override.strip() else None
        )
        prompt = (
            override_prompt
            if override_prompt is not None
            else _build_adapter_prompt(messages, system_prompt=system_prompt)
        )
        command = self._build_command()

        logger.debug(
            "Running Gemini CLI",
            extra={
                "command": shlex.join(command),
                "working_directory": str(working_directory),
                "prompt_length": len(prompt),
            },
        )

        try:
            execution_cwd = (
                self.working_directory if override_prompt is not None else working_directory
            )
            completed = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                # Keep self-review (prompt_override) subprocesses out of the workspace repo
                # to prevent side-effect edits that bypass runtime command capture.
                # For normal runtime turns, use the worker-provided workspace cwd.
                cwd=execution_cwd,
                env=self.env,
                timeout=self.request_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Gemini CLI adapter timed out after {self.request_timeout_seconds}s."
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"Gemini CLI adapter could not start `{self.executable}`: {exc}"
            ) from exc

        if completed.returncode != 0:
            raise RuntimeError(
                "Gemini CLI adapter failed with exit code "
                f"{completed.returncode}. stderr: {_truncate_detail(completed.stderr)} "
                f"stdout: {_truncate_detail(completed.stdout)}"
            )

        raw_output = completed.stdout
        # If we asked for JSON output, try to parse the structured response first.
        try:
            structured = json.loads(raw_output)
            if isinstance(structured, dict) and "response" in structured:
                val = structured["response"]
                raw_output = val if isinstance(val, str) else json.dumps(val)
        except json.JSONDecodeError:
            # Fall back to raw output if it wasn't valid JSON (e.g. CLI warnings)
            pass
        raw_output = raw_output.strip()
        if not raw_output:
            raise RuntimeError("Gemini CLI adapter returned an empty response body.")

        if override_prompt is not None:
            try:
                raw_json = _extract_json(raw_output)
                parsed = json.loads(raw_json)
                normalized = _coerce_step_payload(parsed)
                if normalized is not None:
                    raw_json = json.dumps(normalized)
            except (RuntimeError, json.JSONDecodeError):
                return CliRuntimeStep(
                    kind="final",
                    final_output=raw_output,
                    tool_name=None,
                    tool_input=None,
                )
            try:
                return CliRuntimeStep.model_validate_json(raw_json)
            except Exception:
                return CliRuntimeStep(
                    kind="final",
                    final_output=raw_json,
                    tool_name=None,
                    tool_input=None,
                )

        try:
            raw_json = _extract_json(raw_output)
            parsed = json.loads(raw_json)
            normalized = _coerce_step_payload(parsed)
            if normalized is not None:
                raw_json = json.dumps(normalized)
        except (RuntimeError, json.JSONDecodeError):
            # If we can't extract or parse JSON, treat the entire output as final_output.
            return CliRuntimeStep(
                kind="final",
                final_output=raw_output,
                tool_name=None,
                tool_input=None,
            )

        try:
            return CliRuntimeStep.model_validate_json(raw_json)
        except Exception:
            # Fall back to a 'final' step if the JSON is valid but not a CliRuntimeStep.
            # This allows parsing one-shot review results or mis-shaped outputs gracefully.
            return CliRuntimeStep(
                kind="final",
                final_output=raw_json,
                tool_name=None,
                tool_input=None,
            )
