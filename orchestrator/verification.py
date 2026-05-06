"""Independent verifier helpers for orchestrator verification stages."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from db.enums import WorkerRuntimeMode
from tools.numeric import coerce_positive_int_like
from workers import Worker, WorkerRequest

if TYPE_CHECKING:
    from orchestrator.state import OrchestratorState

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
    """Extract verifier JSON payload from direct JSON or fenced JSON output."""
    stripped = summary.strip()
    if not stripped:
        return None

    try:
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    for match in re.finditer(
        r"```(?:json)?\s*(.*?)\s*```",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        block = match.group(1).strip()
        if not block:
            continue
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _coerce_outcome_status(value: object) -> Literal["passed", "failed", "warning"] | None:
    """Normalize verifier status strings to the supported vocabulary."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized == "passed":
        return "passed"
    if normalized == "failed":
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


def _parse_verifier_summary(summary: str) -> tuple[Literal["passed", "failed", "warning"], str]:
    """Parse verifier model summary into a typed `(status, message)` tuple."""
    payload = _extract_json_payload(summary)
    if payload is not None:
        status = _coerce_outcome_status(payload.get("status"))
        message = payload.get("summary")
        if status is not None and isinstance(message, str) and message.strip():
            return status, message.strip()
        if status is not None:
            return status, "Independent verifier returned status without a summary."

    fallback_status = _fallback_status_from_text(summary)
    preview = summary.strip().replace("\n", " ")
    if len(preview) > _INDEPENDENT_VERIFIER_SUMMARY_MAX_CHARS:
        preview = preview[:_INDEPENDENT_VERIFIER_SUMMARY_MAX_CHARS] + "..."
    if not preview:
        preview = "no summary returned"
    return fallback_status, f"Independent verifier returned unstructured output: {preview}"


async def run_independent_verifier(
    state: OrchestratorState,
    *,
    worker_factory: Mapping[str, Worker] | None,
) -> tuple[Literal["passed", "failed", "warning"], str]:
    """Run independent verifier through native workers in read-only mode."""
    if state.result is None:
        return "warning", "Independent verifier skipped: no worker result available."

    workers = worker_factory or {}
    selected = _pick_verifier_worker(state, workers)
    if selected is None:
        return "warning", "Independent verifier skipped: no verifier worker configured."

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
            worker.run(request, system_prompt=_INDEPENDENT_VERIFIER_SYSTEM_PROMPT),
            timeout=timeout_seconds + _INDEPENDENT_VERIFIER_TIMEOUT_GRACE_SECONDS,
        )
    except TimeoutError:
        return (
            "failed",
            f"Independent verifier timed out after {timeout_seconds}s ({worker_type}).",
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
            "failed",
            f"Independent verifier infrastructure error ({worker_type}): {type(exc).__name__}.",
        )

    if verifier_result.status != "success":
        message = verifier_result.summary or "no summary returned"
        if verifier_result.failure_kind in {"provider_error", "provider_auth", "sandbox_infra"}:
            return (
                "warning",
                f"Independent verifier could not complete ({worker_type}): {message}",
            )
        return (
            "failed",
            f"Independent verifier failed ({worker_type}): {message}",
        )

    parsed_status, parsed_summary = _parse_verifier_summary(verifier_result.summary or "")
    return parsed_status, parsed_summary
