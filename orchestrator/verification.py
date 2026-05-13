"""Independent verifier helpers for orchestrator verification stages."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Literal

from db.enums import WorkerRuntimeMode
from orchestrator.brain import extract_json_block
from orchestrator.state import OrchestratorState
from tools.numeric import coerce_positive_int_like
from workers import Worker, WorkerRequest, WorkerResult

logger = logging.getLogger(__name__)

DEFAULT_INDEPENDENT_VERIFIER_TIMEOUT_SECONDS = 120
_INDEPENDENT_VERIFIER_TIMEOUT_GRACE_SECONDS = 15
_INDEPENDENT_VERIFIER_SUMMARY_MAX_CHARS = 300

_INDEPENDENT_VERIFIER_SYSTEM_PROMPT = """
You are an independent verification agent operating in strict read-only mode.

Requirements:
- Do not edit files.
- Validate the submitted changes by running the most relevant checks.
- Prefer the verification commands provided by TaskSpec when they are applicable.
- If verification cannot be completed, explain why clearly.

Output contract:
- Return a single JSON object only (no markdown fences, no extra prose).
- JSON schema:
  {
    "status": "passed" | "failed" | "warning",
    "summary": "<concise explanation>"
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


def _resolve_independent_verifier_timeout_seconds(state: OrchestratorState) -> int:
    """Resolve timeout budget for the independent verifier run."""
    budget = state.task.budget if isinstance(state.task.budget, dict) else {}
    return (
        coerce_positive_int_like(budget.get("independent_verifier_timeout_seconds"))
        or DEFAULT_INDEPENDENT_VERIFIER_TIMEOUT_SECONDS
    )


def _build_verifier_task_text(state: OrchestratorState) -> str:
    """Build a compact verification task payload for the read-only verifier agent."""
    task_text = state.normalized_task_text or state.task.task_text
    worker_summary = state.result.summary if state.result is not None else ""
    files_changed = state.result.files_changed if state.result is not None else []
    commands = resolve_verification_commands(state)
    lines = [
        _INDEPENDENT_VERIFIER_SYSTEM_PROMPT,
        "",
        "Independently verify the previously completed task in read-only mode.",
        f"Original task: {task_text}",
        "",
        "Execution result context:",
        f"- Worker summary: {worker_summary or 'n/a'}",
        f"- Files changed: {', '.join(files_changed) if files_changed else 'none reported'}",
    ]
    if commands:
        lines.extend(
            [
                "",
                "TaskSpec verification commands to prioritize when applicable:",
                *[f"- {command}" for command in commands],
            ]
        )
    lines.extend(
        [
            "",
            "Return JSON only in the required schema.",
        ]
    )
    return "\n".join(lines)


def _pick_verifier_worker(
    state: OrchestratorState,
    worker_factory: Mapping[str, Worker],
) -> tuple[str, Worker] | None:
    """Select the worker used for independent verifier execution."""
    if not worker_factory:
        return None

    candidate_order: list[str] = []
    if "gemini" in worker_factory:
        candidate_order.append("gemini")
    if "codex" in worker_factory:
        candidate_order.append("codex")
    dispatch_worker = state.dispatch.worker_type
    if (
        dispatch_worker
        and dispatch_worker in worker_factory
        and dispatch_worker not in candidate_order
    ):
        candidate_order.append(dispatch_worker)

    if not candidate_order:
        candidate_order = sorted(worker_factory.keys())

    selected = candidate_order[0]
    return selected, worker_factory[selected]


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


async def run_independent_verifier(
    state: OrchestratorState,
    *,
    worker_factory: Mapping[str, Worker] | None,
) -> tuple[Literal["passed", "failed", "warning"], str, str | None]:
    """Run independent verifier through native workers in read-only mode."""
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
    selected = _pick_verifier_worker(state, workers)
    if selected is None:
        logger.info(
            "Independent verifier skipped: no verifier worker configured",
            extra={"task_id": state.task.task_id},
        )
        return (
            "warning",
            "Independent verifier skipped: no verifier worker configured.",
            "no_verifier_worker",
        )

    worker_type, worker = selected
    timeout_seconds = _resolve_independent_verifier_timeout_seconds(state)

    constraints = dict(state.task.constraints)
    constraints["read_only"] = True
    constraints.pop("granted_permission", None)

    budget = dict(state.task.budget)
    budget["worker_timeout_seconds"] = timeout_seconds

    request = WorkerRequest(
        session_id=state.session.session_id if state.session is not None else None,
        repo_url=state.task.repo_url,
        branch=state.task.branch,
        task_text=_build_verifier_task_text(state),
        memory_context=state.memory.model_dump(),
        task_spec=state.task_spec.model_dump(mode="json") if state.task_spec is not None else None,
        constraints=constraints,
        budget=budget,
        secrets=dict(state.task.secrets),
        tools=state.task.tools,
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
    )

    try:
        verifier_result = await asyncio.wait_for(
            worker.run(request),
            timeout=timeout_seconds + _INDEPENDENT_VERIFIER_TIMEOUT_GRACE_SECONDS,
        )
    except TimeoutError:
        if _internal_tests_passed(state):
            return (
                "warning",
                f"Independent verifier timed out after {timeout_seconds}s ({worker_type}), "
                "but internal tests passed.",
                "infra_verifier_unavailable",
            )
        return (
            "warning",
            f"Independent verifier timed out after {timeout_seconds}s ({worker_type}).",
            "infra_verifier_unavailable",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Independent verifier execution failed unexpectedly",
            exc_info=True,
            extra={"worker_type": worker_type},
        )
        return (
            "warning",
            f"Independent verifier infrastructure error ({worker_type}): {type(exc).__name__}.",
            "infra_verifier_unavailable",
        )

    if verifier_result.status != "success":
        message = verifier_result.summary or "no summary returned"
        if verifier_result.failure_kind in {"provider_error", "provider_auth", "sandbox_infra"}:
            return (
                "warning",
                f"Independent verifier could not complete ({worker_type}): {message}",
                "infra_verifier_unavailable",
            )
        if verifier_result.failure_kind in {"timeout", "tool_runtime", "unknown"}:
            return (
                "warning",
                f"Independent verifier could not complete ({worker_type}): {message}",
                "infra_verifier_unavailable",
            )
        return (
            "warning",
            f"Independent verifier could not complete ({worker_type}): {message}",
            "infra_verifier_unavailable",
        )

    parsed_status, parsed_summary = _parse_verifier_result(verifier_result)
    return parsed_status, parsed_summary, None


async def run_deterministic_verification(
    state: OrchestratorState,
    *,
    worker_factory: Mapping[str, Worker] | None,
) -> tuple[Literal["passed", "failed", "warning"], str]:
    """Run explicit verification commands deterministically in the sandbox."""
    if state.result is None:
        return "warning", "Deterministic verification skipped: no worker result available."

    commands = resolve_verification_commands(state)
    if not commands:
        return "passed", "No explicit verification commands defined."

    workers = worker_factory or {}
    # We prefer the shell worker if available.
    if "shell" not in workers:
        return "warning", "Deterministic verification skipped: no 'shell' worker available."

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
    # Build a script from commands
    script = "\n".join(commands)

    constraints = dict(state.task.constraints)
    if state.result is not None and state.result.diff_text:
        constraints["apply_diff_text"] = state.result.diff_text

    request = WorkerRequest(
        session_id=state.session.session_id if state.session is not None else None,
        repo_url=state.task.repo_url,
        branch=state.task.branch,
        task_text=script,
        budget={"worker_timeout_seconds": timeout_seconds},
        secrets=dict(state.task.secrets),
        constraints=constraints,
        runtime_mode=WorkerRuntimeMode.SHELL,
    )

    try:
        verifier_result = await asyncio.wait_for(
            worker.run(request),
            timeout=timeout_seconds + _INDEPENDENT_VERIFIER_TIMEOUT_GRACE_SECONDS,
        )
    except TimeoutError:
        if _internal_tests_passed(state):
            return (
                "warning",
                f"Deterministic verification timed out after {timeout_seconds}s, "
                "but internal tests passed.",
            )
        return (
            "failed",
            f"Deterministic verification timed out after {timeout_seconds}s.",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Deterministic verification execution failed unexpectedly", exc_info=True)
        return (
            "failed",
            f"Deterministic verification infrastructure error: {type(exc).__name__}.",
        )

    if verifier_result.status != "success":
        message = verifier_result.summary or "no summary returned"
        logger.warning(
            "Deterministic verification commands failed",
            extra={
                "session_id": state.session.session_id if state.session else None,
                "task_id": state.task.task_id,
            },
        )
        return "failed", f"Deterministic verification failed: {message}"

    logger.info(
        "Deterministic verification commands passed",
        extra={
            "session_id": state.session.session_id if state.session else None,
            "task_id": state.task.task_id,
        },
    )
    return "passed", "Explicit verification commands passed."
