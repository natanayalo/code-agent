"""Reusable one-shot native-agent CLI runner."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Final, Literal

from apps.observability import (
    NATIVE_AGENT_COMMAND_ATTRIBUTE,
    NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH,
    SPAN_KIND_AGENT,
    add_current_span_event,
    inject_w3c_trace_context_env,
    set_current_span_attribute,
    start_optional_span,
    with_span_kind,
)
from sandbox.redact import sanitize_command
from workers.adapter_utils import truncate_detail_keep_tail
from workers.base import ArtifactReference
from workers.cli_runtime import collect_changed_files_from_repo_path
from workers.failure_taxonomy import find_infra_failure_marker
from workers.llm_tracing import set_llm_span_output, with_llm_span
from workers.native_agent_artifacts import (
    DEFAULT_NATIVE_AGENT_ARTIFACTS_DIR,
    _collect_diff_text,
    _collect_standard_artifacts,
    _copy_artifact,
    _write_artifact,
)
from workers.native_agent_finalize import _finalize_native_agent_run
from workers.native_agent_json import (
    _extract_business_json_payload,
    _split_llm_output_and_metadata,
)
from workers.native_agent_messages import (
    _normalize_stream_payload,
    _read_final_message,
    _stdout_fallback_final_message,
)
from workers.native_agent_models import NativeAgentRunRequest, NativeAgentRunResult
from workers.native_agent_tracing import _record_span_data

logger = logging.getLogger(__name__)

_FINAL_MESSAGE_FIELDS: Final = (
    "error",
    "final_output",
    "summary",
    "message",
    "content",
    "response",
)
_LLM_METADATA_ATTR_PREFIX: Final[str] = "code_agent.native.llm_wrapper"
_JSON_PAYLOAD_ATTR_PREFIX: Final[str] = "code_agent.native.json_payload"

# Standardized system environment variables that are safe to propagate to the sandbox.
SAFE_SYSTEM_ENV_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "TERM",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "PIP_CERT",
        "SSL_CERT_FILE",
        "GPG_KEY",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "PWD",
        "USER",
        "HOSTNAME",
    }
)

# Environment variable prefixes that must never be passed to the sandbox
# unless explicitly overridden.
SENSITIVE_ENV_PREFIX_DENYLIST: Final[frozenset[str]] = frozenset(
    {
        "AWS_",
        "GCP_",
        "OTEL_",
        "PHOENIX_",
        "DATABASE_",
        "POSTGRES_",
        "TELEGRAM_",
        "GEMINI_",
        "OPENROUTER_",
        "CODE_AGENT_",
    }
)

# Explicit auth-home keys that are safe to propagate even when HOME is isolated.
ALLOWED_AUTH_HOME_KEYS: Final[frozenset[str]] = frozenset({"CODEX_HOME", "GEMINI_HOME"})


_SIGNAL_EXIT_CODES: Final = {
    132: "SIGILL",
    -4: "SIGILL",
    134: "SIGABRT",
    -6: "SIGABRT",
    137: "SIGKILL",
    -9: "SIGKILL",
    135: "SIGBUS",
    138: "SIGBUS",
    -7: "SIGBUS",
    -10: "SIGBUS",
    139: "SIGSEGV",
    -11: "SIGSEGV",
}


def _build_effective_env(request: NativeAgentRunRequest) -> dict[str, str]:
    effective_env: dict[str, str] = {
        k: v for k, v in os.environ.items() if k.upper() in SAFE_SYSTEM_ENV_ALLOWLIST
    }
    agent_home = request.workspace_path / ".agent_home"
    agent_home.mkdir(parents=True, exist_ok=True)
    agent_home_value = str(agent_home)
    effective_env["HOME"] = agent_home_value

    if request.env:
        for k, v in request.env.items():
            k_upper = k.upper()
            if k_upper == "HOME":
                logger.warning("Native agent runner dropped protected environment key: %s", k)
                continue
            if k_upper in ALLOWED_AUTH_HOME_KEYS:
                effective_env[k] = v
                continue
            is_denied = any(k_upper.startswith(prefix) for prefix in SENSITIVE_ENV_PREFIX_DENYLIST)
            if is_denied:
                logger.warning("Native agent runner dropped sensitive environment key: %s", k)
                continue
            effective_env[k] = v

    effective_env.update(
        {
            "HOME": agent_home_value,
            "CODEX_HOME": "/root/.codex",
            "GEMINI_HOME": "/root/.gemini",
            "CODE_AGENT_ENABLE_TRACING": "0",
            "CODE_AGENT_ENABLE_TASK_SERVICE": "0",
            "CODE_AGENT_INDEPENDENT_VERIFIER_ENABLED": "0",
            "DATABASE_URL": f"sqlite:///{request.workspace_path.as_posix()}/.sandbox.db",
            "TELEGRAM_BOT_TOKEN": "",
        }
    )
    return effective_env


def _build_friction_report_dict(
    source: str,
    description: str,
    impact: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "description": description,
        "impact": impact,
        "context": context or {},
    }


def _determine_exit_status(
    completed_returncode: int, final_message: str | None, stderr_text: str
) -> tuple[Literal["success", "failure", "error"], str, list[dict[str, Any]]]:
    friction_reports: list[dict[str, Any]] = []
    if completed_returncode == 0:
        return (
            "success",
            final_message or "Native agent run completed successfully.",
            friction_reports,
        )

    summary = f"Native agent command exited with code {completed_returncode}."
    status: Literal["success", "failure", "error"] = "failure"

    stderr_tail = truncate_detail_keep_tail(stderr_text, max_characters=8192)
    if marker := find_infra_failure_marker(stderr_tail):
        friction_reports.append(
            _build_friction_report_dict(
                source="sandbox",
                description=f"Sandbox infra crash detected: {marker}",
                impact="blocked",
                context={"marker": marker, "exit_code": completed_returncode},
            )
        )
        return "error", f"SANDBOX_INFRA: detected shell crash ({marker})", friction_reports
    if "requires user confirmation" in stderr_tail.lower():
        return (
            "error",
            "SANDBOX_INFRA: shell command blocked (requires user confirmation in non-interactive mode)",  # noqa: E501
            friction_reports,
        )
    if "tool" in stderr_tail.lower() and "not found" in stderr_tail.lower():
        truncated_stderr = truncate_detail_keep_tail(stderr_tail, max_characters=1024)
        lines = truncated_stderr.strip().splitlines()
        last_line = lines[-1] if lines else "unknown error"
        friction_reports.append(
            _build_friction_report_dict(
                source="sandbox",
                description=f"Tool registry mismatch: {last_line}",
                impact="blocked",
                context={"last_line": last_line, "exit_code": completed_returncode},
            )
        )
        return (
            "error",
            f"SANDBOX_INFRA: tool registry mismatch detected ({last_line})",
            friction_reports,
        )
    if completed_returncode in _SIGNAL_EXIT_CODES:
        sig_name = _SIGNAL_EXIT_CODES[completed_returncode]
        friction_reports.append(
            _build_friction_report_dict(
                source="sandbox",
                description=f"Sandbox infra crash detected via signal: {sig_name}",
                impact="blocked",
                context={"signal": sig_name, "exit_code": completed_returncode},
            )
        )
        return "error", f"SANDBOX_INFRA: detected shell crash ({sig_name})", friction_reports

    if any(err in stderr_text for err in ["ECONNRESET", "socket hang up", "ETIMEDOUT"]):
        friction_reports.append(
            _build_friction_report_dict(
                source="tooling",
                description="Native agent command failed with network retry exhaustion.",
                impact="blocked",
                context={"exit_code": completed_returncode},
            )
        )

    return status, summary, friction_reports


def _process_llm_metadata(llm_metadata: dict[str, Any] | None) -> None:  # type: ignore[name-defined]
    if llm_metadata is None:
        return
    set_current_span_attribute(
        f"{_LLM_METADATA_ATTR_PREFIX}.present",
        True,
    )
    if isinstance(llm_metadata.get("session_id"), str):
        set_current_span_attribute(
            f"{_LLM_METADATA_ATTR_PREFIX}.session_id",
            llm_metadata["session_id"],
        )
    set_current_span_attribute(
        f"{_LLM_METADATA_ATTR_PREFIX}.json",
        truncate_detail_keep_tail(
            json.dumps(llm_metadata, default=str),
            max_characters=NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH,
        ),
    )
    add_current_span_event(
        "code_agent.native.metadata_detected",
        {
            "has_session_id": isinstance(llm_metadata.get("session_id"), str),
            "metadata_keys_count": len(llm_metadata),
        },
    )


def _handle_network_error_retry(
    output_text: str,
    retry_count: int,
    max_retries: int,
    command_text: str,
    retry_reason: str,
) -> bool:
    if (
        any(err in output_text for err in ["ECONNRESET", "socket hang up", "ETIMEDOUT"])
        and retry_count < max_retries
    ):
        new_retry_count = retry_count + 1
        wait_seconds = 2**new_retry_count
        logger.warning(
            f"Native agent {retry_reason}, retrying...",
            extra={
                "retry_count": new_retry_count,
                "wait_seconds": wait_seconds,
                "command": command_text,
            },
        )
        add_current_span_event(
            "code_agent.native.run_retry",
            {
                "retry_count": new_retry_count,
                "wait_seconds": wait_seconds,
                "retry_reason": retry_reason,
            },
        )
        time.sleep(wait_seconds)
        return True
    return False


def _handle_native_agent_timeout(
    request: NativeAgentRunRequest,
    exc: subprocess.TimeoutExpired,
    retry_count: int,
    max_retries: int,
    command_text: str,
    started_at: float,
    artifact_root: Path,  # type: ignore[name-defined]
    events_path: Path | None,  # type: ignore[name-defined]
) -> tuple[bool, NativeAgentRunResult | None]:
    """Handle timeout. Returns (should_retry, run_result)."""
    stdout_text = _normalize_stream_payload(exc.stdout)
    stderr_text = _normalize_stream_payload(exc.stderr)
    output_text = stdout_text + stderr_text

    friction_reports = []
    if _handle_network_error_retry(
        output_text, retry_count, max_retries, command_text, "timed out with network error"
    ):
        return True, None
    else:
        friction_reports.append(
            _build_friction_report_dict(
                source="tooling",
                description=f"Native agent command timed out after {request.timeout_seconds}s.",
                impact="blocked",
                context={"timeout_seconds": request.timeout_seconds},
            )
        )

    artifacts: list[ArtifactReference] = []
    try:
        artifacts.extend(
            _collect_standard_artifacts(
                artifact_root=artifact_root,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                events_path=events_path,
            )
        )
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as e:
        logger.debug("Native agent runner failed to collect timeout artifacts: %s", e)
        logger.exception(
            "Native agent runner failed while collecting timeout artifacts.",
            extra={"command": command_text},
        )
        friction_reports.append(
            _build_friction_report_dict(
                source="orchestrator",
                description=f"Native agent runner failed to collect artifacts on timeout: {e}",
                impact="blocked",
                context={"error_type": type(e).__name__},
            )
        )

    return False, _finalize_native_agent_run(
        request=request,
        status="error",
        summary=f"Native agent command timed out after {request.timeout_seconds}s.",
        command_text=command_text,
        started_at=started_at,
        timed_out=True,
        stdout=stdout_text,
        stderr=stderr_text,
        artifacts=artifacts,
        friction_reports=friction_reports,
    )


def _execute_native_agent_subprocess(
    request: NativeAgentRunRequest,
    repo_path: Path,  # type: ignore[name-defined]
    command_text: str,
    started_at: float,
    artifact_root: Path,  # type: ignore[name-defined]
    events_path: Path | None,  # type: ignore[name-defined]
) -> subprocess.CompletedProcess[str] | NativeAgentRunResult:
    """Run the native agent subprocess with retries for network errors."""
    max_retries = 2
    retry_count = 0

    while retry_count <= max_retries:
        try:
            with with_llm_span(
                tracer_name="workers.native_agent_runner",
                span_name=f"native_agent_run.{request.span_kind.lower()}",
                input_data=request.prompt,
                task_id=request.task_id,
                session_id=request.session_id,
                kind=request.span_kind,
            ):
                effective_env = _build_effective_env(request)

                completed = subprocess.run(
                    request.command,
                    input=request.prompt,
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=repo_path,
                    env=inject_w3c_trace_context_env(effective_env),
                    timeout=request.timeout_seconds,
                )
                llm_output, llm_metadata = _split_llm_output_and_metadata(completed.stdout or "")
                set_llm_span_output(llm_output)
                _process_llm_metadata(llm_metadata)

            # Check for network-like errors in stdout/stderr even if it finished
            output_text = (completed.stdout or "") + (completed.stderr or "")
            if _handle_network_error_retry(
                output_text, retry_count, max_retries, command_text, "encountered network error"
            ):
                retry_count += 1
                continue

            return completed
        except subprocess.TimeoutExpired as exc:
            should_retry, result = _handle_native_agent_timeout(
                request,
                exc,
                retry_count,
                max_retries,
                command_text,
                started_at,
                artifact_root,
                events_path,
            )
            if should_retry:
                retry_count += 1
                continue
            assert result is not None
            return result

    raise RuntimeError("Unexpected escape from retry loop")


def _collect_optional_diff_and_changed_files(
    request: NativeAgentRunRequest,
    repo_path: Path,  # type: ignore[name-defined]
    artifact_root: Path,  # type: ignore[name-defined]
    artifacts: list[ArtifactReference],
) -> tuple[list[str], str | None]:
    """Collect git diff and changed files if requested."""
    files_changed = (
        collect_changed_files_from_repo_path(
            repo_path,
            timeout_seconds=request.changed_files_timeout_seconds,
        )
        if request.collect_changed_files
        else []
    )
    diff_text = (
        _collect_diff_text(repo_path=repo_path, timeout_seconds=request.diff_timeout_seconds)
        if request.collect_diff
        else None
    )
    if diff_text is not None:
        artifacts.append(
            _write_artifact(
                artifact_root=artifact_root,
                file_name="diff.patch",
                content=diff_text,
                name="native-agent-diff",
                artifact_type="diff",
            )
        )
    return files_changed, diff_text


def _build_native_agent_artifacts(
    request: NativeAgentRunRequest,
    repo_path: Path,  # type: ignore[name-defined]
    artifact_root: Path,  # type: ignore[name-defined]
    events_path: Path | None,  # type: ignore[name-defined]
    final_message_path: Path | None,  # type: ignore[name-defined]
    stdout_text: str,
    stderr_text: str,
) -> tuple[list[ArtifactReference], str | None, list[str], str | None]:
    """Returns artifacts, final_message, files_changed, diff_text."""
    artifacts: list[ArtifactReference] = []
    artifacts.extend(
        _collect_standard_artifacts(
            artifact_root=artifact_root,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            events_path=events_path,
        )
    )

    final_message_artifact = (
        _copy_artifact(
            artifact_root=artifact_root,
            source_path=final_message_path,
            file_name="final-message.txt",
            name="native-agent-final-message",
            artifact_type="result_summary",
        )
        if final_message_path is not None
        else None
    )
    if final_message_artifact is not None:
        artifacts.append(final_message_artifact)

    final_message = _read_final_message(final_message_path) if final_message_path else None
    if final_message is None:
        final_message = _stdout_fallback_final_message(stdout_text)

    files_changed, diff_text = _collect_optional_diff_and_changed_files(
        request, repo_path, artifact_root, artifacts
    )
    return artifacts, final_message, files_changed, diff_text


def _collect_native_agent_results(
    request: NativeAgentRunRequest,
    completed: subprocess.CompletedProcess[str],
    repo_path: Path,  # type: ignore[name-defined]
    command_text: str,
    started_at: float,
    artifact_root: Path,  # type: ignore[name-defined]
    events_path: Path | None,  # type: ignore[name-defined]
    final_message_path: Path | None,  # type: ignore[name-defined]
) -> NativeAgentRunResult:
    """Collect artifacts, determine status, and finalize the result."""
    stdout_text = completed.stdout or ""
    stderr_text = completed.stderr or ""
    artifacts: list[ArtifactReference] = []

    try:
        artifacts, final_message, files_changed, diff_text = _build_native_agent_artifacts(
            request,
            repo_path,
            artifact_root,
            events_path,
            final_message_path,
            stdout_text,
            stderr_text,
        )

        status, summary, friction_reports = _determine_exit_status(
            completed.returncode, final_message, stderr_text
        )

        json_payload, json_payload_source, json_payload_rejected_reason = (
            _extract_business_json_payload(
                final_message=final_message,
                stdout_text=stdout_text,
                response_format=request.response_format,
                response_schema=request.response_schema,
            )
        )

        return _finalize_native_agent_run(
            request=request,
            status=status,
            summary=summary,
            command_text=command_text,
            started_at=started_at,
            timed_out=False,
            exit_code=completed.returncode,
            stdout=stdout_text,
            stderr=stderr_text,
            final_message=final_message,
            diff_text=diff_text,
            files_changed=files_changed,
            artifacts=artifacts,
            json_payload=json_payload,
            json_payload_source=json_payload_source,
            json_payload_rejected_reason=json_payload_rejected_reason,
            friction_reports=friction_reports,
        )
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
        logger.debug("Native agent runner artifact collection failed: %s", exc)
        logger.exception(
            "Native agent runner failed while collecting artifacts or metadata.",
            extra={
                "command": command_text,
                "exit_code": completed.returncode if completed else None,
            },
        )
        friction_reports = [
            _build_friction_report_dict(
                source="orchestrator",
                description=f"Native agent runner failed while collecting artifacts: {exc}",
                impact="blocked",
                context={"error_type": type(exc).__name__, "error": str(exc)},
            )
        ]
        return _finalize_native_agent_run(
            request=request,
            status="error",
            summary=f"Native agent runner failed while collecting artifacts: {exc}",
            command_text=command_text,
            started_at=started_at,
            timed_out=False,
            exit_code=completed.returncode if completed else None,
            stdout=stdout_text,
            stderr=stderr_text,
            artifacts=artifacts,
            friction_reports=friction_reports,
        )


def _setup_native_agent_paths(
    request: NativeAgentRunRequest,
) -> tuple[Path, Path | None, Path | None, Path]:  # type: ignore[name-defined]
    repo_path = request.repo_path.expanduser().resolve()
    workspace_path = request.workspace_path.expanduser().resolve()
    final_message_path = (
        request.final_message_path.expanduser().resolve()
        if request.final_message_path is not None
        else None
    )
    events_path = request.events_path.expanduser().resolve() if request.events_path else None
    artifact_root = (
        workspace_path
        / DEFAULT_NATIVE_AGENT_ARTIFACTS_DIR
        / f"run-{int(time.time() * 1000)}-{time.monotonic_ns()}"
    )
    artifact_root.mkdir(parents=True, exist_ok=True)
    return repo_path, final_message_path, events_path, artifact_root


def run_native_agent(request: NativeAgentRunRequest) -> NativeAgentRunResult:
    """Run one native-agent CLI command and capture stable worker-facing outputs."""

    if not request.command:
        raise ValueError("NativeAgentRunRequest.command must include at least one argv token.")

    repo_path, final_message_path, events_path, artifact_root = _setup_native_agent_paths(request)

    command_text = shlex.join(request.command)
    started_at = time.perf_counter()
    with start_optional_span(
        tracer_name="workers.native_agent_runner",
        span_name="native_agent_run",
        attributes=with_span_kind(SPAN_KIND_AGENT),
        task_id=request.task_id,
        session_id=request.session_id,
    ):
        _record_span_data(
            input_data=request.prompt,
            redactor=request.redactor,
        )
        set_current_span_attribute(
            NATIVE_AGENT_COMMAND_ATTRIBUTE, sanitize_command(command_text, request.redactor)
        )

        try:
            result_or_completed = _execute_native_agent_subprocess(
                request=request,
                repo_path=repo_path,
                command_text=command_text,
                started_at=started_at,
                artifact_root=artifact_root,
                events_path=events_path,
            )
        except OSError as exc:
            return _finalize_native_agent_run(
                request=request,
                status="error",
                summary=(f"Native agent command could not start `{request.command[0]}`: {exc}"),
                command_text=command_text,
                started_at=started_at,
                timed_out=False,
            )

        if isinstance(result_or_completed, NativeAgentRunResult):
            return result_or_completed

        completed = result_or_completed
        if completed is None:
            # Should be unreachable if the loop exited via break
            return _finalize_native_agent_run(
                request=request,
                status="error",
                summary="Native agent runner failed unexpectedly (process result missing).",
                command_text=command_text,
                started_at=started_at,
                timed_out=False,
            )

        return _collect_native_agent_results(
            request=request,
            completed=completed,
            repo_path=repo_path,
            command_text=command_text,
            started_at=started_at,
            artifact_root=artifact_root,
            events_path=events_path,
            final_message_path=final_message_path,
        )
