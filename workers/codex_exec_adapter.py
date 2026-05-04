"""Concrete Codex CLI adapter for the shared multi-turn runtime."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from workers.adapter_parsing import parse_cli_runtime_step_or_final
from workers.adapter_prompts import (
    CODEX_ADAPTER_IDENTITY_LINE,
    CODEX_SCHEMA_RESPONSE_LINE,
    build_final_example_json,
    build_runtime_transcript_prompt,
    build_tool_call_example_json,
)
from workers.adapter_utils import (
    coerce_positive_int,
    normalize_prompt_override,
    truncate_detail_keep_tail,
)
from workers.cli_runtime import CliRuntimeAdapter, CliRuntimeMessage, CliRuntimeStep
from workers.llm_tracing import set_llm_span_output, with_llm_span
from workers.prompt import build_runtime_adapter_tool_guidance_lines
from workers.subprocess_env import build_codex_subprocess_env

DEFAULT_CODEX_EXECUTABLE: Final[str] = "codex"
DEFAULT_CODEX_SANDBOX_MODE: Final[str] = "read-only"
DEFAULT_CODEX_REQUEST_TIMEOUT_SECONDS: Final[int] = 120
_DETAIL_PREVIEW_CHARACTERS: Final[int] = 1200

CODEX_EXECUTABLE_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_CLI_BIN"
CODEX_MODEL_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_MODEL"
CODEX_PROFILE_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_PROFILE"
CODEX_TIMEOUT_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_TIMEOUT_SECONDS"
CODEX_SANDBOX_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_SANDBOX"
TRACER_NAME: Final[str] = "workers.codex"


def _build_adapter_prompt(
    messages: Sequence[CliRuntimeMessage],
    *,
    system_prompt: str | None = None,
) -> str:
    """Build the single-shot prompt sent to `codex exec` for one runtime turn."""
    tool_guidance_lines = build_runtime_adapter_tool_guidance_lines(system_prompt=system_prompt)
    return build_runtime_transcript_prompt(
        identity_line=CODEX_ADAPTER_IDENTITY_LINE,
        response_instruction_line=CODEX_SCHEMA_RESPONSE_LINE,
        tool_call_example=f"- {build_tool_call_example_json()}",
        final_example=f"- {build_final_example_json()}",
        tool_guidance_lines=tool_guidance_lines,
        messages=messages,
        system_prompt=system_prompt,
        json_output_rule="- Do not wrap the JSON in Markdown or add any extra prose.",
    )


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
        resolved_env = os.environ if env is None else env
        self.executable = executable
        self.model = model.strip() if model is not None and model.strip() else None
        self.profile = profile.strip() if profile is not None and profile.strip() else None
        self.sandbox_mode = sandbox_mode.strip() or DEFAULT_CODEX_SANDBOX_MODE
        self.request_timeout_seconds = coerce_positive_int(
            request_timeout_seconds, default=DEFAULT_CODEX_REQUEST_TIMEOUT_SECONDS
        )
        self.working_directory = Path(working_directory or tempfile.gettempdir()).expanduser()
        self.config_overrides = tuple(
            override.strip()
            for override in config_overrides
            if isinstance(override, str) and override.strip()
        )
        self.env = build_codex_subprocess_env(resolved_env)

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
            request_timeout_seconds=coerce_positive_int(
                resolved_env.get(CODEX_TIMEOUT_ENV_VAR),
                default=DEFAULT_CODEX_REQUEST_TIMEOUT_SECONDS,
            ),
            env=resolved_env,
        )

    def _build_command(
        self,
        *,
        output_schema_path: Path | None,
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
            "--output-last-message",
            str(output_message_path),
            "--ephemeral",
            "-C",
            str(working_directory or self.working_directory),
        ]
        if output_schema_path is not None:
            command.extend(["--output-schema", str(output_schema_path)])
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
        system_prompt: str | None = None,
        prompt_override: str | None = None,
        working_directory: Path | None = None,
    ) -> CliRuntimeStep:
        """Ask the Codex CLI for the next runtime step."""
        override_prompt = normalize_prompt_override(prompt_override)
        prompt = (
            override_prompt
            if override_prompt is not None
            else _build_adapter_prompt(messages, system_prompt=system_prompt)
        )
        with tempfile.TemporaryDirectory(prefix="code-agent-codex-step-") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            output_message_path = temp_dir / "last_message.json"
            schema_path: Path | None = None
            if override_prompt is None:
                schema_path = temp_dir / "cli_runtime_step.schema.json"
                schema_path.write_text(
                    json.dumps(_codex_output_schema(), indent=2, sort_keys=True),
                    encoding="utf-8",
                )

            try:
                with with_llm_span(
                    tracer_name=TRACER_NAME,
                    span_name="codex.exec",
                    input_data=prompt,
                ):
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
                        set_llm_span_output(completed.stdout)
                    except subprocess.TimeoutExpired as exc:
                        raise RuntimeError(
                            "Codex CLI adapter timed out after "
                            f"{self.request_timeout_seconds}s while selecting the next runtime step."  # noqa: E501
                        ) from exc
            except OSError as exc:
                raise RuntimeError(
                    f"Codex CLI adapter could not start `{self.executable}`: {exc}"
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
                    "Codex CLI adapter failed with exit code "
                    f"{completed.returncode}. stderr: {stderr_preview} "
                    f"stdout: {stdout_preview}"
                )

            if not output_message_path.exists():
                raise RuntimeError(
                    "Codex CLI adapter completed without writing the final message file. "
                    f"stdout: {stdout_preview} "
                    f"stderr: {stderr_preview}"
                )

            raw_output = output_message_path.read_text(encoding="utf-8").strip()
            if not raw_output:
                raise RuntimeError(
                    "Codex CLI adapter wrote an empty final message file. "
                    f"stdout: {stdout_preview} "
                    f"stderr: {stderr_preview}"
                )

            try:
                parsed = json.loads(raw_output)
                if isinstance(parsed, dict) and isinstance(parsed.get("tool_input"), dict):
                    parsed["tool_input"] = json.dumps(parsed["tool_input"])
                    raw_output = json.dumps(parsed)
            except json.JSONDecodeError:
                pass

            # Fall back to a 'final' step if the payload does not validate as CliRuntimeStep.
            return parse_cli_runtime_step_or_final(raw_output)
