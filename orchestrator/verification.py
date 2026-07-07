"""Independent verifier helpers for orchestrator verification stages."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from collections.abc import Mapping
from typing import Any, Literal

from apps.observability import (
    NATIVE_AGENT_STDERR_ATTRIBUTE,
    NATIVE_AGENT_STDOUT_ATTRIBUTE,
    OPENINFERENCE_SPAN_KIND_ATTRIBUTE,
    SPAN_KIND_TOOL,
    add_current_span_event,
    set_current_span_attribute,
    set_span_input_output,
    start_optional_span,
)
from db.enums import WorkerRuntimeMode
from orchestrator.brain import extract_json_block
from orchestrator.state import OrchestratorState, is_task_read_only
from tools import ToolPermissionLevel
from tools.numeric import coerce_positive_int_like
from workers import Worker, WorkerRequest, WorkerResult
from workers.constants import DEFAULT_VERIFIER_TIMEOUT_SECONDS
from workers.review_context import pack_inspection_context

logger = logging.getLogger(__name__)

DEFAULT_INDEPENDENT_VERIFIER_TIMEOUT_SECONDS = DEFAULT_VERIFIER_TIMEOUT_SECONDS
_INDEPENDENT_VERIFIER_TIMEOUT_GRACE_SECONDS = 15
_INDEPENDENT_VERIFIER_SUMMARY_MAX_CHARS = 300
_VERIFICATION_PLACEHOLDER_PREVIEW_MAX = 3
_DETERMINISTIC_OUTPUT_PREVIEW_MAX_CHARS = 500
_DETERMINISTIC_SHADOW_GUARD_EXIT_CODE = 97
_PYTHON_MODULE_SHADOW_GUARDS: Mapping[str, tuple[str, ...]] = {
    "pytest": ("pytest.py", "pytest"),
}

_INDEPENDENT_VERIFIER_SYSTEM_PROMPT = """
You are an autonomous CI/QA Verification Agent. Your goal is to rigorously validate the
changes submitted by a coding worker.

Operational Philosophy:
- **Think like a QA Engineer**: Don't just trust that the code runs; look for logical gaps,
  edge cases, and regressions that the developer might have missed.
- **Red Team Mentality**: Actively try to find bugs. Use your tools to probe the logic
  (e.g., creating temporary test scripts, running targeted commands, or checking
  boundary conditions).
- **Strict Read-Only Mode**: You must NOT modify the codebase. All your verification actions
  (tests, linting, exploration) must be non-destructive.

Requirements:
- **Mandatory Baseline**: Always verify that the "standard" tests or checks for this
  repository are passing (e.g., pytest, npm test, lint).
- **Exploratory Probing**: Use tools like `git diff`, `read_file`, and `ls` to identify
  high-risk areas. If you suspect a hidden bug, prove its existence by running a targeted
  command.
- **Coverage & Quality**: If the task objective specified quality metrics (e.g., "at least
  90% coverage"), ensure you run the commands that report these metrics and validate the
  results.
- **No Self-Repair**: Do not attempt to fix bugs yourself. If you find a regression, report
  it clearly in your summary so the developer can fix it.

Output contract:
- Return a single JSON object only (no markdown fences, no extra prose).
- JSON schema:
  {
    "status": "passed" | "failed" | "warning",
    "summary": "<concise explanation of your findings, including evidence of any regressions found>"
  }
""".strip()


_INDEPENDENT_VERIFIER_READ_ONLY_PROMPT = """
You are an autonomous QA Verification Agent. Your goal is to rigorously evaluate the
findings and analysis submitted by a research or scouting worker.

Operational Philosophy:
- **Think like an Auditor**: Do not just trust that the worker's summary is correct.
  Validate that their findings actually answer the user's core question or analysis goal.
- **Strict Read-Only Mode**: You must NOT modify the codebase. All your verification actions
  (exploring the repo, reading files) must be non-destructive.

Requirements:
- **Goal Satisfaction**: Verify that the worker's summary directly addresses the task requirements.
- **Factual Correctness**: If the worker claims a file contains X, use `read_file` or `grep_search`
  to verify that X is actually there.
- **Incomplete Findings**: If the worker missed critical files or misunderstood the architecture,
  report it clearly so they can try again.

Output contract:
- Return a single JSON object only (no markdown fences, no extra prose).
- JSON schema:
  {
    "status": "passed" | "failed" | "warning",
    "summary": "<concise explanation of your findings, why worker succeeded/failed>"
  }
""".strip()


def _normalize_verification_commands(raw: object) -> list[str]:
    """Normalize verification command inputs into stripped command strings."""
    if isinstance(raw, str):
        commands: list[str] = []
        current_command = ""
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line:
                if current_command:
                    commands.append(current_command.strip())
                    current_command = ""
                continue

            current_command = f"{current_command} {line}".strip() if current_command else line
            if current_command.endswith("\\"):
                current_command = current_command[:-1].rstrip()
                continue

            commands.append(current_command.strip())
            current_command = ""

        if current_command:
            commands.append(current_command.strip())
        return commands
    if not isinstance(raw, list | tuple):
        return []
    normalized_commands: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        command = item.strip()
        if command:
            normalized_commands.append(command)
    return normalized_commands


def resolve_verification_commands(state: OrchestratorState) -> list[str]:
    """Resolve verifier commands from task spec first, then constraints fallback."""
    if state.task_spec is not None:
        commands = _normalize_verification_commands(state.task_spec.verification_commands)
        if commands:
            return commands
    return _normalize_verification_commands(state.task.constraints.get("verification_commands"))


def _is_placeholder_verification_command(command: str) -> bool:
    """Return True when a command looks like a template placeholder."""
    stripped = command.strip()
    if not stripped:
        return False
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    lowered = stripped.lower()
    if "<project-specific" in lowered or "<project specific" in lowered:
        return True
    return False


def split_verification_commands(
    commands: list[str],
) -> tuple[list[str], list[str]]:
    """Split verification commands into executable and placeholder/template commands."""
    executable: list[str] = []
    placeholders: list[str] = []
    for command in commands:
        if _is_placeholder_verification_command(command):
            placeholders.append(command)
        else:
            executable.append(command)
    return executable, placeholders


def _looks_like_python_executable(token: str) -> bool:
    executable = token.rsplit("/", 1)[-1]
    return executable in {"python", "python3"} or executable.startswith("python3.")


def _python_module_invocations(command: str) -> list[str]:
    """Return modules invoked by simple `python -m ...` segments in a shell command."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []

    modules: list[str] = []
    for index, token in enumerate(tokens):
        if not _looks_like_python_executable(token):
            continue
        cursor = index + 1
        while cursor < len(tokens):
            current = tokens[cursor]
            if current == "-m":
                if cursor + 1 < len(tokens):
                    modules.append(tokens[cursor + 1].split(":", 1)[0])
                break
            if current == "-c" or not current.startswith("-"):
                break
            cursor += 1
    return modules


def _shadow_guard_modules_for_commands(commands: list[str]) -> list[str]:
    guarded_modules: set[str] = set()
    for command in commands:
        for module_name in _python_module_invocations(command):
            root_module = module_name.split(".", 1)[0]
            if root_module in _PYTHON_MODULE_SHADOW_GUARDS:
                guarded_modules.add(root_module)
    return sorted(guarded_modules)


def _python_module_shadow_guard_script(commands: list[str]) -> list[str]:
    guarded_modules = _shadow_guard_modules_for_commands(commands)
    if not guarded_modules:
        return []

    lines = [
        "# code-agent deterministic verifier preflight: block repo-local python module shadowing.",
    ]
    for module_name in guarded_modules:
        shadow_paths = _PYTHON_MODULE_SHADOW_GUARDS[module_name]
        test_expr = " || ".join(f"[ -e {shlex.quote(path)} ]" for path in shadow_paths)
        display_paths = " or ".join(shadow_paths)
        message = (
            f"Deterministic verification blocked: python -m {module_name} may be shadowed "
            f"by repo-local {display_paths}. Remove or rename the shadowing path, or use "
            "a dependency-safe verifier command."
        )
        lines.extend(
            [
                f"if {test_expr}; then",
                f"  echo {shlex.quote(message)} >&2",
                f"  exit {_DETERMINISTIC_SHADOW_GUARD_EXIT_CODE}",
                "fi",
            ]
        )
    return lines


def _with_python_module_shadow_guard_metadata(
    metadata: dict[str, Any] | None, commands: list[str]
) -> dict[str, Any] | None:
    guarded_modules = _shadow_guard_modules_for_commands(commands)
    if not guarded_modules:
        return metadata
    output = dict(metadata or {})
    output["python_module_shadow_guard"] = {
        "modules": guarded_modules,
        "exit_code": _DETERMINISTIC_SHADOW_GUARD_EXIT_CODE,
    }
    return output


def _resolve_independent_verifier_timeout_seconds(state: OrchestratorState) -> int:
    """Resolve timeout budget for the independent verifier run."""
    budget = state.task.budget if isinstance(state.task.budget, dict) else {}
    return (
        coerce_positive_int_like(budget.get("independent_verifier_timeout_seconds"))
        or DEFAULT_INDEPENDENT_VERIFIER_TIMEOUT_SECONDS
    )


def _build_verifier_task_text(state: OrchestratorState) -> str:
    """Build a compact verification task payload for the read-only verifier agent."""
    inspection_context = pack_inspection_context(
        task_text=state.normalized_task_text or state.task.task_text,
        worker_summary=(state.result.summary or "") if state.result is not None else "",
        files_changed=state.result.files_changed if state.result is not None else [],
        inspection_commands=resolve_verification_commands(state),
        # Verifier starts tool-first, so we don't provide a diff in the initial prompt.
        diff_text=None,
    )
    lines = [
        _INDEPENDENT_VERIFIER_SYSTEM_PROMPT,
        "",
        "Independently verify the previously completed task in read-only mode.",
        "",
        inspection_context,
        "",
        "Return JSON only in the required schema.",
    ]
    return "\n".join(lines)


def _get_verifier_workers(
    state: OrchestratorState,
    worker_factory: Mapping[str, Worker],
) -> list[tuple[str, Worker]]:
    """Get an ordered list of workers to use for independent verifier execution."""
    if not worker_factory:
        return []

    candidate_order: list[str] = []
    if "antigravity" in worker_factory:
        candidate_order.append("antigravity")
    if "codex" in worker_factory:
        candidate_order.append("codex")
    if "openrouter" in worker_factory:
        candidate_order.append("openrouter")

    dispatch_worker = state.dispatch.worker_type
    if (
        dispatch_worker
        and dispatch_worker in worker_factory
        and dispatch_worker not in candidate_order
    ):
        candidate_order.append(dispatch_worker)

    if not candidate_order:
        candidate_order = sorted(worker_factory.keys())

    # Filter out non-LLM workers
    candidate_order = [w for w in candidate_order if w != "shell"]

    return [(w, worker_factory[w]) for w in candidate_order]


def _extract_json_payload(summary: str) -> dict[str, object] | None:
    """Extract verifier JSON payload using the hardened orchestrator helper."""
    normalized_json = extract_json_block(summary)
    if not normalized_json:
        return None

    try:
        payload = json.loads(normalized_json)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    return None


def _coerce_outcome_status(value: object) -> Literal["passed", "failed", "warning"] | None:
    """Normalize verifier status strings to the supported vocabulary."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized == "passed" or normalized == "success":
        return "passed"
    if normalized == "failed" or normalized == "failure" or normalized == "error":
        return "failed"
    if normalized == "warning":
        return "warning"
    return None


def _fallback_status_from_text(summary: str) -> Literal["passed", "failed", "warning"]:
    """Best-effort fallback status extraction when JSON parsing fails."""
    lowered = summary.lower()
    if "failed" in lowered or "regression" in lowered or "error" in lowered:
        return "failed"
    if "pass" in lowered or "ok" in lowered or "success" in lowered:
        return "passed"
    return "warning"


def _parse_verifier_result(
    result: WorkerResult,
) -> tuple[Literal["passed", "failed", "warning"], str]:
    """Parse verifier worker result into a typed `(status, message)` tuple."""
    # 1. Prioritize structured payload if available
    payload = result.json_payload
    if not isinstance(payload, dict):
        payload = _extract_json_payload(result.summary or "")

    if payload is not None:
        status = _coerce_outcome_status(payload.get("status"))
        message = payload.get("summary") or payload.get("message")
        if status is not None and isinstance(message, str) and message.strip():
            return status, message.strip()
        if status is not None:
            return status, "Independent verifier returned status without a summary."

    # 2. Fall back to text-based heuristics
    summary = result.summary or ""
    fallback_status = _fallback_status_from_text(summary)
    preview = summary.strip().replace("\n", " ")
    if len(preview) > _INDEPENDENT_VERIFIER_SUMMARY_MAX_CHARS:
        preview = preview[:_INDEPENDENT_VERIFIER_SUMMARY_MAX_CHARS] + "..."
    if not preview:
        preview = "no summary returned"
    return fallback_status, f"Independent verifier returned unstructured output: {preview}"


def _internal_tests_passed(state: OrchestratorState) -> bool:
    """Check if the previous worker's reported test results all passed."""
    if state.result is None:
        return False
    # If no tests reported, rely on worker status
    if not state.result.test_results:
        return state.result.status == "success"
    return all(r.status == "passed" for r in state.result.test_results)


def _build_independent_verifier_request(
    state: OrchestratorState,
    timeout_seconds: int,
) -> WorkerRequest:
    constraints = dict(state.task.constraints)
    constraints["read_only"] = False
    if constraints.get("granted_permission") != ToolPermissionLevel.WORKSPACE_WRITE:
        constraints.pop("granted_permission", None)

    budget = dict(state.task.budget)
    budget["worker_timeout_seconds"] = timeout_seconds

    return WorkerRequest(
        session_id=state.session.session_id if state.session is not None else None,
        repo_url=state.task.repo_url,
        branch=state.task.branch,
        workspace_id=state.dispatch.workspace_id
        or (state.result.workspace_id if state.result else None),
        read_only=False,
        task_text=_build_verifier_task_text(state),
        memory_context=state.memory.model_dump(),
        task_spec=state.task_spec.model_dump(mode="json") if state.task_spec is not None else None,
        constraints=constraints,
        budget=budget,
        secrets=dict((state.task.secrets or {}) | {"POETRY_VIRTUALENVS_IN_PROJECT": "true"}),
        tools=state.task.tools,
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
    )


def _handle_verifier_worker_failure(
    verifier_result: WorkerResult,
    worker_type: str,
    state: OrchestratorState,
    is_last_worker: bool,
) -> tuple[Literal["passed", "failed", "warning"] | None, str | None, str | None]:
    message = verifier_result.summary or "no summary returned"
    if verifier_result.failure_kind in {
        "provider_error",
        "provider_auth",
        "sandbox_infra",
        "timeout",
        "model_error",
        "unknown",
    }:
        logger.warning(
            f"Independent verifier hit {verifier_result.failure_kind} for {worker_type}: {message}",
            extra={"task_id": state.task.task_id},
        )
        if is_last_worker:
            return (
                "warning",
                "Independent verifier could not complete (all fallbacks exhausted). "
                f"Last error ({worker_type}): {message}",
                "infra_verifier_unavailable",
            )
        return None, None, None
    else:
        return (
            "warning",
            f"Independent verifier could not complete ({worker_type}): {message}",
            "infra_verifier_unavailable",
        )


def _handle_verifier_worker_exception(
    exc: Exception,
    worker_type: str,
    state: OrchestratorState,
    is_last_worker: bool,
) -> tuple[Literal["passed", "failed", "warning"] | None, str | None, str | None]:
    if isinstance(exc, TimeoutError):
        logger.warning(
            f"Independent verifier timed out for {worker_type}",
            extra={"task_id": state.task.task_id},
        )
        if is_last_worker:
            if _internal_tests_passed(state):
                return (
                    "warning",
                    f"Independent verifier timed out ({worker_type}), but internal tests passed.",
                    "infra_verifier_unavailable",
                )
            return (
                "warning",
                f"Independent verifier timed out ({worker_type}).",
                "infra_verifier_unavailable",
            )
        return None, None, None
    else:
        logger.warning(
            "Independent verifier execution failed unexpectedly",
            exc_info=True,
            extra={"worker_type": worker_type, "task_id": state.task.task_id},
        )
        if is_last_worker:
            return (
                "warning",
                f"Independent verifier infrastructure error ({worker_type}): {type(exc).__name__}.",
                "infra_verifier_unavailable",
            )
        return None, None, None


async def _execute_verifier_worker(
    worker_type: str,
    worker: Worker,
    request: WorkerRequest,
    state: OrchestratorState,
    timeout_seconds: int,
    is_last_worker: bool,
) -> tuple[Literal["passed", "failed", "warning"] | None, str | None, str | None]:
    try:
        with start_optional_span(
            tracer_name="orchestrator.verification",
            span_name=f"independent_verifier.{worker_type}",
            task_id=state.task.task_id,
            session_id=state.session.session_id if state.session else None,
            attempt=state.attempt_count,
            task_kind=state.task_kind,
            route_reason=state.route.route_reason if state.route else None,
            verification_summary=state.verification.summary if state.verification else None,
            attributes={OPENINFERENCE_SPAN_KIND_ATTRIBUTE: SPAN_KIND_TOOL},
        ):
            set_span_input_output(input_data=request.task_text)
            prompt = (
                _INDEPENDENT_VERIFIER_READ_ONLY_PROMPT
                if is_task_read_only(state)
                else _INDEPENDENT_VERIFIER_SYSTEM_PROMPT
            )
            verifier_result = await asyncio.wait_for(
                worker.run(request, system_prompt=prompt),
                timeout=timeout_seconds + _INDEPENDENT_VERIFIER_TIMEOUT_GRACE_SECONDS,
            )

            if verifier_result.stdout:
                set_current_span_attribute(NATIVE_AGENT_STDOUT_ATTRIBUTE, verifier_result.stdout)
            if verifier_result.stderr:
                set_current_span_attribute(NATIVE_AGENT_STDERR_ATTRIBUTE, verifier_result.stderr)
            set_span_input_output(input_data=None, output_data=verifier_result.summary)

        if verifier_result.status != "success":
            return _handle_verifier_worker_failure(
                verifier_result, worker_type, state, is_last_worker
            )

        parsed_status, parsed_summary = _parse_verifier_result(verifier_result)
        return parsed_status, parsed_summary, None

    except TimeoutError as exc:
        return _handle_verifier_worker_exception(exc, worker_type, state, is_last_worker)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _handle_verifier_worker_exception(exc, worker_type, state, is_last_worker)


async def run_independent_verifier(
    state: OrchestratorState,
    *,
    worker_factory: Mapping[str, Worker] | None,
) -> tuple[Literal["passed", "failed", "warning"], str, str | None]:
    """Run independent verifier through native workers in read-only mode with fallback."""
    if state.result is None:
        return "warning", "Independent verifier skipped: no worker result available.", "no_result"

    workers = worker_factory or {}
    logger.info(
        "Starting independent verifier check",
        extra={
            "session_id": state.session.session_id if state.session else None,
            "task_id": state.task.task_id,
        },
    )

    verifier_workers = _get_verifier_workers(state, workers)
    if not verifier_workers:
        logger.info(
            "Independent verifier skipped: no verifier worker configured",
            extra={"task_id": state.task.task_id},
        )
        return (
            "warning",
            "Independent verifier skipped: no verifier worker configured.",
            "no_verifier_worker",
        )

    timeout_seconds = _resolve_independent_verifier_timeout_seconds(state)
    request = _build_independent_verifier_request(state, timeout_seconds)

    for i, (worker_type, worker) in enumerate(verifier_workers):
        is_last = i == len(verifier_workers) - 1
        status, msg, code = await _execute_verifier_worker(
            worker_type, worker, request, state, timeout_seconds, is_last
        )
        if status is not None:
            assert msg is not None
            return status, msg, code

    return "warning", "Independent verifier failed to execute.", "infra_verifier_unavailable"


def _build_deterministic_verification_request(
    state: OrchestratorState, commands: list[str], timeout_seconds: int
) -> WorkerRequest:
    script = "\n".join(["set -e", *_python_module_shadow_guard_script(commands), *commands])
    workspace_id = state.dispatch.workspace_id or (
        state.result.workspace_id if state.result else None
    )
    constraints = dict(state.task.constraints)
    if not workspace_id and state.result is not None and state.result.diff_text:
        constraints["apply_diff_text"] = state.result.diff_text

    return WorkerRequest(
        session_id=state.session.session_id if state.session is not None else None,
        repo_url=state.task.repo_url,
        branch=state.task.branch,
        workspace_id=workspace_id,
        read_only=state.task.constraints.get("read_only", False),
        task_text=script,
        budget={"worker_timeout_seconds": timeout_seconds},
        secrets=dict(state.task.secrets),
        constraints=constraints,
        runtime_mode=WorkerRuntimeMode.SHELL,
    )


def _extract_verification_placeholder_metadata(
    placeholder_commands: list[str], executable_commands: list[str]
) -> dict[str, Any] | None:
    if not placeholder_commands:
        return None
    metadata = {
        "placeholder_commands_filtered": True,
        "placeholder_count": len(placeholder_commands),
        "placeholder_preview": placeholder_commands[:_VERIFICATION_PLACEHOLDER_PREVIEW_MAX],
        "executable_count": len(executable_commands),
    }
    set_current_span_attribute("code_agent.verification.placeholder_filtered", True)
    set_current_span_attribute(
        "code_agent.verification.placeholder_filtered_count", len(placeholder_commands)
    )
    add_current_span_event(
        "code_agent.verification.commands.filtered",
        {
            "filtered_count": len(placeholder_commands),
            "executable_count": len(executable_commands),
        },
    )
    return metadata


def _with_deterministic_command_metadata(
    metadata: dict[str, Any] | None,
    *,
    commands: list[str],
    status: Literal["passed", "failed", "warning"],
    summary: str,
    stdout: str | None = None,
    stderr: str | None = None,
    exit_code: int | None = None,
) -> dict[str, Any]:
    """Attach deterministic verifier command facts to reusable metadata."""
    output = dict(metadata or {})
    output.update(
        {
            "commands": list(commands),
            "status": status,
            "summary": summary,
        }
    )
    if status == "passed":
        output["passed_commands"] = list(commands)
    elif status == "failed":
        output["failed_commands"] = list(commands)
    if exit_code is not None:
        output["exit_code"] = exit_code
    if stdout:
        output["stdout_preview"] = stdout[:_DETERMINISTIC_OUTPUT_PREVIEW_MAX_CHARS]
    if stderr:
        output["stderr_preview"] = stderr[:_DETERMINISTIC_OUTPUT_PREVIEW_MAX_CHARS]
    return output


async def _run_deterministic_verification_request(
    worker: Worker,
    request: WorkerRequest,
    state: OrchestratorState,
    timeout_seconds: int,
) -> WorkerResult:
    """Execute the shell verifier request under a traced span."""
    with start_optional_span(
        tracer_name="orchestrator.verification",
        span_name="deterministic_verification",
        task_id=state.task.task_id,
        session_id=state.session.session_id if state.session else None,
        attempt=state.attempt_count,
        task_kind=state.task_kind,
        route_reason=state.route.route_reason if state.route else None,
        verification_summary=state.verification.summary if state.verification else None,
        attributes={OPENINFERENCE_SPAN_KIND_ATTRIBUTE: SPAN_KIND_TOOL},
    ):
        set_span_input_output(input_data=request.task_text)
        verifier_result = await asyncio.wait_for(
            worker.run(request),
            timeout=timeout_seconds + _INDEPENDENT_VERIFIER_TIMEOUT_GRACE_SECONDS,
        )

        if verifier_result.stdout:
            set_current_span_attribute(NATIVE_AGENT_STDOUT_ATTRIBUTE, verifier_result.stdout)
        if verifier_result.stderr:
            set_current_span_attribute(NATIVE_AGENT_STDERR_ATTRIBUTE, verifier_result.stderr)
        set_span_input_output(input_data=None, output_data=verifier_result.summary)
        return verifier_result


def _deterministic_timeout_result(
    state: OrchestratorState,
    *,
    timeout_seconds: int,
    metadata: dict[str, Any] | None,
    commands: list[str],
) -> tuple[Literal["passed", "failed", "warning"], str, dict[str, Any] | None]:
    if _internal_tests_passed(state):
        summary = (
            f"Deterministic verification timed out after {timeout_seconds}s, "
            "but internal tests passed."
        )
        status: Literal["passed", "failed", "warning"] = "warning"
    else:
        summary = f"Deterministic verification timed out after {timeout_seconds}s."
        status = "failed"
    return (
        status,
        summary,
        _with_deterministic_command_metadata(
            metadata,
            commands=commands,
            status=status,
            summary=summary,
        ),
    )


def _deterministic_exception_result(
    exc: Exception,
    *,
    metadata: dict[str, Any] | None,
    commands: list[str],
) -> tuple[Literal["passed", "failed", "warning"], str, dict[str, Any] | None]:
    summary = f"Deterministic verification infrastructure error: {type(exc).__name__}."
    return (
        "failed",
        summary,
        _with_deterministic_command_metadata(
            metadata,
            commands=commands,
            status="failed",
            summary=summary,
        ),
    )


def _last_command_exit_code(result: WorkerResult) -> int | None:
    return result.commands_run[-1].exit_code if result.commands_run else None


def _worker_stream(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _deterministic_worker_failure_result(
    verifier_result: WorkerResult,
    state: OrchestratorState,
    *,
    metadata: dict[str, Any] | None,
    commands: list[str],
) -> tuple[Literal["passed", "failed", "warning"], str, dict[str, Any] | None]:
    message = verifier_result.summary or "no summary returned"
    summary = f"Deterministic verification failed: {message}"
    logger.warning(
        "Deterministic verification commands failed",
        extra={
            "session_id": state.session.session_id if state.session else None,
            "task_id": state.task.task_id,
        },
    )
    return (
        "failed",
        summary,
        _with_deterministic_command_metadata(
            metadata,
            commands=commands,
            status="failed",
            summary=summary,
            stdout=_worker_stream(verifier_result.stdout),
            stderr=_worker_stream(verifier_result.stderr),
            exit_code=_last_command_exit_code(verifier_result),
        ),
    )


def _deterministic_worker_success_result(
    verifier_result: WorkerResult,
    state: OrchestratorState,
    *,
    metadata: dict[str, Any] | None,
    commands: list[str],
) -> tuple[Literal["passed", "failed", "warning"], str, dict[str, Any] | None]:
    logger.info(
        "Deterministic verification commands passed",
        extra={
            "session_id": state.session.session_id if state.session else None,
            "task_id": state.task.task_id,
        },
    )
    summary = "Explicit verification commands passed."
    return (
        "passed",
        summary,
        _with_deterministic_command_metadata(
            metadata,
            commands=commands,
            status="passed",
            summary=summary,
            stdout=_worker_stream(verifier_result.stdout),
            stderr=_worker_stream(verifier_result.stderr),
            exit_code=_last_command_exit_code(verifier_result),
        ),
    )


async def _execute_deterministic_verification_worker(
    worker: Worker,
    request: WorkerRequest,
    state: OrchestratorState,
    timeout_seconds: int,
    metadata: dict[str, Any] | None,
    commands: list[str],
) -> tuple[Literal["passed", "failed", "warning"], str, dict[str, Any] | None]:
    try:
        verifier_result = await _run_deterministic_verification_request(
            worker, request, state, timeout_seconds
        )
    except TimeoutError:
        return _deterministic_timeout_result(
            state,
            timeout_seconds=timeout_seconds,
            metadata=metadata,
            commands=commands,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Deterministic verification execution failed unexpectedly", exc_info=True)
        return _deterministic_exception_result(exc, metadata=metadata, commands=commands)

    if verifier_result.status != "success":
        return _deterministic_worker_failure_result(
            verifier_result, state, metadata=metadata, commands=commands
        )

    return _deterministic_worker_success_result(
        verifier_result, state, metadata=metadata, commands=commands
    )


async def run_deterministic_verification(
    state: OrchestratorState,
    *,
    worker_factory: Mapping[str, Worker] | None,
) -> tuple[Literal["passed", "failed", "warning"], str, dict[str, Any] | None]:
    """Run explicit verification commands deterministically in the sandbox."""
    if state.result is None:
        return "warning", "Deterministic verification skipped: no worker result available.", None

    commands = resolve_verification_commands(state)
    if not commands:
        return "passed", "No explicit verification commands defined.", None

    executable_commands, placeholder_commands = split_verification_commands(commands)
    metadata = _extract_verification_placeholder_metadata(placeholder_commands, executable_commands)

    commands = executable_commands
    if not commands:
        combined_metadata = dict(metadata or {})
        combined_metadata["skip_reason_code"] = "verification_commands_placeholder_only"
        combined_metadata["commands"] = []
        combined_metadata["status"] = "warning"
        return (
            "warning",
            (
                "Deterministic verification skipped: all configured verification "
                "commands were placeholders."
            ),
            combined_metadata,
        )
    metadata = _with_python_module_shadow_guard_metadata(metadata, commands)

    workers = worker_factory or {}
    if "shell" not in workers:
        summary = "Deterministic verification skipped: no 'shell' worker available."
        return (
            "warning",
            summary,
            _with_deterministic_command_metadata(
                metadata,
                commands=commands,
                status="warning",
                summary=summary,
            ),
        )

    worker = workers["shell"]
    timeout_seconds = _resolve_independent_verifier_timeout_seconds(state)

    logger.info(
        "Running deterministic verification commands",
        extra={
            "session_id": state.session.session_id if state.session else None,
            "task_id": state.task.task_id,
            "command_count": len(commands),
        },
    )

    request = _build_deterministic_verification_request(state, commands, timeout_seconds)
    return await _execute_deterministic_verification_worker(
        worker, request, state, timeout_seconds, metadata, commands
    )
