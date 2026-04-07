"""Concrete Gemini CLI adapter for the shared multi-turn runtime."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from workers.cli_runtime import CliRuntimeAdapter, CliRuntimeMessage, CliRuntimeStep

DEFAULT_GEMINI_EXECUTABLE: Final[str] = "gemini"
DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS: Final[int] = 120
_DETAIL_PREVIEW_CHARACTERS: Final[int] = 1200

GEMINI_EXECUTABLE_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_CLI_BIN"
GEMINI_MODEL_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_MODEL"
GEMINI_TIMEOUT_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_TIMEOUT_SECONDS"


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
    """Build the prompt sent to the Gemini CLI for one runtime turn."""
    lines = [
        "You are the Gemini runtime adapter for a bounded coding worker.",
        (
            "Read the transcript below and return exactly one JSON object "
            "with no surrounding text, no markdown fences, and no explanation."
        ),
        "Choose one of two actions:",
        CliRuntimeStep(
            kind="tool_call",
            tool_name="execute_bash",
            tool_input="<one shell command>",
            final_output=None,
        ).model_dump_json(),
        CliRuntimeStep(
            kind="final",
            final_output="<final summary for the user>",
            tool_name=None,
            tool_input=None,
        ).model_dump_json(),
        "Rules:",
        "- Use only the `execute_bash` tool.",
        "- Request one focused shell command at a time.",
        "- If the transcript already contains enough information to finish, return `final`.",
        "- If the latest tool result failed, adapt to that failure instead of repeating blindly.",
        "- Return ONLY a raw JSON object. No markdown fences, no extra explanation.",
        "",
        "## Runtime Transcript",
    ]
    for index, message in enumerate(messages, start=1):
        lines.extend((_message_heading(message, index=index), message.content, ""))
    return "\n".join(lines).rstrip()


def _extract_json(text: str) -> str:
    """Extract the outermost JSON object from a Gemini CLI response.

    Uses find/rfind to locate the outermost braces so that nested objects,
    trailing prose, and markdown-fenced responses are all handled correctly.
    """
    stripped = text.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1].strip()
    raise RuntimeError(f"No JSON object found in Gemini CLI response: {_truncate_detail(stripped)}")


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
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.executable = executable
        self.model = model.strip() if model is not None and model.strip() else None
        self.request_timeout_seconds = _coerce_positive_int(
            request_timeout_seconds, default=DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS
        )
        self.env = dict(env) if env is not None else None

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
        """Build the gemini CLI argv for one adapter turn.

        The prompt is supplied via stdin. The ``--model`` flag is added when a
        model override is configured.
        """
        command = [self.executable]
        if self.model is not None:
            command.extend(["--model", self.model])
        return command

    def next_step(
        self,
        messages: Sequence[CliRuntimeMessage],
        *,
        working_directory: Path | None = None,  # noqa: ARG002 — context only, not used by CLI
    ) -> CliRuntimeStep:
        """Ask the Gemini CLI for the next runtime step."""
        prompt = _build_adapter_prompt(messages)
        command = self._build_command()

        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.request_timeout_seconds,
                env=self.env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "Gemini CLI adapter timed out after "
                f"{self.request_timeout_seconds}s while selecting the next runtime step."
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

        raw_json = _extract_json(completed.stdout)
        try:
            return CliRuntimeStep.model_validate_json(raw_json)
        except Exception as exc:
            raise RuntimeError(
                "Gemini CLI adapter returned a response that did not match "
                f"CliRuntimeStep: {_truncate_detail(raw_json)}"
            ) from exc
