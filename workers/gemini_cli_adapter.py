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

from workers.adapter_parsing import final_cli_runtime_step, parse_cli_runtime_step_or_final
from workers.adapter_prompts import (
    DEFAULT_RAW_JSON_ONLY_RULE,
    DEFAULT_STRICT_KIND_RULE,
    DEFAULT_TOOL_INPUT_ENCODE_RULE,
    GEMINI_ADAPTER_IDENTITY_LINE,
    build_final_example_json,
    build_runtime_transcript_prompt,
    build_tool_call_example_json,
    json_only_response_line,
)
from workers.adapter_utils import (
    coerce_positive_int,
    normalize_prompt_override,
    truncate_detail_keep_tail,
)
from workers.cli_runtime import CliRuntimeAdapter, CliRuntimeMessage, CliRuntimeStep
from workers.llm_tracing import (
    normalize_llm_output,
    set_llm_span_output,
    with_llm_span,
)
from workers.prompt import build_runtime_adapter_tool_guidance_lines
from workers.subprocess_env import build_gemini_subprocess_env

DEFAULT_GEMINI_EXECUTABLE: Final[str] = "gemini"
DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS: Final[int] = 120
_DETAIL_PREVIEW_CHARACTERS: Final[int] = 1200

GEMINI_EXECUTABLE_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_CLI_BIN"
GEMINI_MODEL_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_MODEL"
GEMINI_TIMEOUT_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_TIMEOUT_SECONDS"
TRACER_NAME: Final[str] = "workers.gemini"

logger = logging.getLogger(__name__)


def _build_adapter_prompt(
    messages: Sequence[CliRuntimeMessage],
    *,
    system_prompt: str | None = None,
) -> str:
    """Build the prompt sent to the Gemini CLI for one runtime turn."""
    tool_guidance_lines = build_runtime_adapter_tool_guidance_lines(system_prompt=system_prompt)
    return build_runtime_transcript_prompt(
        identity_line=GEMINI_ADAPTER_IDENTITY_LINE,
        response_instruction_line=json_only_response_line(include_transcript_reference=True),
        tool_call_example=build_tool_call_example_json(
            tool_input=(
                '<tool input as a single escaped string, e.g. "ls -la" or '
                '"{\\"op\\":\\"status\\"}" >'
            )
        ),
        final_example=build_final_example_json(),
        tool_guidance_lines=tool_guidance_lines,
        messages=messages,
        system_prompt=system_prompt,
        json_output_rule=DEFAULT_RAW_JSON_ONLY_RULE,
        tool_input_rule=DEFAULT_TOOL_INPUT_ENCODE_RULE,
        pre_rules_lines=(DEFAULT_STRICT_KIND_RULE,),
    )


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
    raise RuntimeError(
        "No JSON object found in Gemini CLI response: "
        f"{truncate_detail_keep_tail(stripped, max_characters=_DETAIL_PREVIEW_CHARACTERS)}"
    )


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
        self.request_timeout_seconds = coerce_positive_int(
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
            request_timeout_seconds=coerce_positive_int(
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
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> CliRuntimeStep:
        """Ask the Gemini CLI for the next runtime step."""
        override_prompt = normalize_prompt_override(prompt_override)
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
            with with_llm_span(
                tracer_name=TRACER_NAME,
                span_name="gemini.chat",
                input_data=prompt,
                task_id=task_id,
                session_id=session_id,
            ):
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
                set_llm_span_output(normalize_llm_output(completed.stdout))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Gemini CLI adapter timed out after {self.request_timeout_seconds}s."
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"Gemini CLI adapter could not start `{self.executable}`: {exc}"
            ) from exc

        stderr_preview = truncate_detail_keep_tail(
            completed.stderr,
            max_characters=_DETAIL_PREVIEW_CHARACTERS,
        )
        stdout_preview = truncate_detail_keep_tail(
            completed.stdout,
            max_characters=_DETAIL_PREVIEW_CHARACTERS,
        )

        if completed.returncode != 0:
            raise RuntimeError(
                "Gemini CLI adapter failed with exit code "
                f"{completed.returncode}. stderr: {stderr_preview} "
                f"stdout: {stdout_preview}"
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
                return final_cli_runtime_step(raw_output)
            return parse_cli_runtime_step_or_final(raw_json)

        try:
            raw_json = _extract_json(raw_output)
            parsed = json.loads(raw_json)
            normalized = _coerce_step_payload(parsed)
            if normalized is not None:
                raw_json = json.dumps(normalized)
        except (RuntimeError, json.JSONDecodeError):
            # If we can't extract or parse JSON, treat the entire output as final_output.
            return final_cli_runtime_step(raw_output)

        # Fall back to a 'final' step if the JSON is valid but not a CliRuntimeStep.
        # This allows parsing one-shot review results or mis-shaped outputs gracefully.
        return parse_cli_runtime_step_or_final(raw_json)
