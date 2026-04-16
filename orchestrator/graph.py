"""LangGraph workflow skeleton for the orchestrator happy path."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from db.base import utc_now
from db.enums import TimelineEventType
from orchestrator.state import (
    ApprovalCheckpoint,
    OrchestratorState,
    RouteDecision,
    SessionStateUpdate,
    TaskTimelineEventState,
    VerificationReport,
    VerificationReportItem,
    WorkerDispatch,
    WorkerType,
)
from workers import Worker, WorkerRequest, WorkerResult

logger = logging.getLogger(__name__)

ORCHESTRATOR_NODE_SEQUENCE = (
    "ingest_task",
    "classify_task",
    "load_memory",
    "choose_worker",
    "check_approval",
    "await_approval",
    "dispatch_job",
    "await_result",
    "verify_result",
    "summarize_result",
    "persist_memory",
)

DESTRUCTIVE_TASK_MARKERS = (
    "delete file",
    "delete files",
    "destroy workspace",
    "drop database",
    "drop table",
    "git clean",
    "git reset",
    "purge data",
    "rm -rf",
    "wipe data",
)

DEFAULT_ORCHESTRATOR_TIMEOUT_SECONDS = 330
ORCHESTRATOR_TIMEOUT_GRACE_SECONDS = 30


def _coerce_positive_int(value: Any) -> int | None:
    """Parse positive integer-like values for timeout-budget overrides."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        try:
            coerced = int(value)
        except (OverflowError, ValueError):
            return None
        return coerced if coerced > 0 else None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            coerced = int(float(normalized))
        except (OverflowError, ValueError):
            return None
        return coerced if coerced > 0 else None
    return None


def _resolve_orchestrator_timeout_seconds(state: OrchestratorState) -> int:
    """Resolve the outer worker timeout envelope from the task budget."""
    budget = state.task.budget

    explicit_timeout = _coerce_positive_int(budget.get("orchestrator_timeout_seconds"))
    if explicit_timeout is not None:
        return explicit_timeout

    worker_timeout_seconds = _coerce_positive_int(budget.get("worker_timeout_seconds"))
    if worker_timeout_seconds is None:
        max_minutes = _coerce_positive_int(budget.get("max_minutes"))
        if max_minutes is not None:
            worker_timeout_seconds = max_minutes * 60

    if worker_timeout_seconds is not None:
        return worker_timeout_seconds + ORCHESTRATOR_TIMEOUT_GRACE_SECONDS

    return DEFAULT_ORCHESTRATOR_TIMEOUT_SECONDS


def _timed_out_worker_result(timeout_seconds: int) -> WorkerResult:
    """Build a structured timeout result for the outer orchestrator envelope."""
    return WorkerResult(
        status="failure",
        summary=(
            "Worker execution exceeded the orchestrator timeout envelope "
            f"({timeout_seconds}s) and was cancelled."
        ),
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="inspect_workspace_artifacts",
    )


def _cancelled_worker_result() -> WorkerResult:
    """Build a structured result for an externally cancelled worker run."""
    return WorkerResult(
        status="failure",
        summary="Worker execution was cancelled before it returned a result.",
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="await_manual_follow_up",
    )


def _unexpected_worker_error_result(exc: Exception) -> WorkerResult:
    """Build a structured result for unexpected worker crashes."""
    detail = str(exc).strip()
    summary = (
        f"Worker execution crashed unexpectedly: {type(exc).__name__}: {detail}"
        if detail
        else f"Worker execution crashed unexpectedly: {type(exc).__name__}."
    )
    return WorkerResult(
        status="error",
        summary=summary,
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="inspect_worker_configuration",
    )


def _consume_worker_task_result(
    worker_task: asyncio.Task[WorkerResult],
    *,
    worker_type: str,
    session_id: str | None,
) -> None:
    """Drain a background worker task result so cleanup never leaks task exceptions."""
    try:
        worker_task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception(
            "Worker task raised while cancellation cleanup was settling",
            extra={
                "session_id": session_id,
                "worker_type": worker_type,
            },
        )


async def _settle_cancelled_worker_task(
    worker_task: asyncio.Task[WorkerResult],
    *,
    worker_type: str,
    session_id: str | None,
    grace_period_seconds: int = 3,
) -> WorkerResult | None:
    """Cancel the task and optionally wait for it to yield a graceful partial result."""
    worker_task.cancel()

    try:
        return await asyncio.wait_for(asyncio.shield(worker_task), timeout=grace_period_seconds)
    except (TimeoutError, asyncio.CancelledError):
        pass
    except Exception:
        logger.warning(
            "Unexpected exception while waiting for graceful worker cancellation",
            exc_info=True,
            extra={"session_id": session_id, "worker_type": worker_type},
        )
        pass

    if worker_task.done() and not worker_task.cancelled():
        try:
            return worker_task.result()
        except Exception:
            logger.warning(
                "Unexpected exception while extracting worker task result after cancellation",
                exc_info=True,
                extra={"session_id": session_id, "worker_type": worker_type},
            )
            pass

    if not worker_task.done():
        worker_task.add_done_callback(
            lambda task: _consume_worker_task_result(
                task,
                worker_type=worker_type,
                session_id=session_id,
            )
        )
    return None


async def _await_worker_with_timeout(
    worker: Worker,
    request: WorkerRequest,
    *,
    worker_type: str,
    session_id: str | None,
    timeout_seconds: int,
) -> tuple[WorkerResult, str]:
    """Run a worker behind the outer orchestrator timeout/cancel envelope."""

    async def run_worker() -> WorkerResult:
        return await worker.run(request)

    worker_task: asyncio.Task[WorkerResult] = asyncio.create_task(run_worker())
    try:
        result = await asyncio.wait_for(asyncio.shield(worker_task), timeout=timeout_seconds)
    except TimeoutError:
        logger.warning(
            "Worker execution exceeded the orchestrator timeout envelope",
            extra={
                "session_id": session_id,
                "worker_type": worker_type,
                "timeout_seconds": timeout_seconds,
            },
        )
        partial_result = await _settle_cancelled_worker_task(
            worker_task,
            worker_type=worker_type,
            session_id=session_id,
        )
        if partial_result is not None:
            return (
                partial_result,
                (f"worker timed out but yielded partial state " f"after {timeout_seconds}s"),
            )

        return _timed_out_worker_result(
            timeout_seconds
        ), f"worker timed out after {timeout_seconds}s"
    except asyncio.CancelledError:
        logger.warning(
            "Worker execution was cancelled at the orchestrator boundary",
            extra={
                "session_id": session_id,
                "worker_type": worker_type,
            },
        )
        partial_result = await _settle_cancelled_worker_task(
            worker_task,
            worker_type=worker_type,
            session_id=session_id,
        )
        if partial_result is not None:
            return partial_result, "worker execution cancelled but yielded partial state"

        return _cancelled_worker_result(), "worker execution cancelled"
    except Exception as exc:
        logger.exception(
            "Worker execution crashed unexpectedly at the orchestrator boundary",
            extra={
                "session_id": session_id,
                "worker_type": worker_type,
            },
        )
        return _unexpected_worker_error_result(exc), "worker crashed unexpectedly"
    return result, "worker result received"


def _ensure_state(state: OrchestratorState | dict[str, Any]) -> OrchestratorState:
    """Normalize raw graph input into the typed orchestrator state."""
    if isinstance(state, OrchestratorState):
        return state
    return OrchestratorState.model_validate(state)


def _progress_update(state: OrchestratorState, message: str) -> list[str]:
    """Append a progress message while preserving prior updates."""
    return [*state.progress_updates, message]


def _timeline_event(
    state: OrchestratorState,
    event_type: str | TimelineEventType,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
) -> list[TaskTimelineEventState]:
    """Append a structured timeline event while preserving prior events."""

    return [
        *state.timeline_events,
        TaskTimelineEventState(
            event_type=str(event_type),
            attempt_number=state.attempt_count,
            message=message,
            payload=payload,
            created_at=utc_now(),
        ),
    ]


def _classify_task_kind(task_text: str) -> str:
    """Apply a small heuristic classifier for the workflow skeleton."""
    normalized_text = task_text.lower()
    if any(keyword in normalized_text for keyword in ("refactor", "architecture", "design")):
        return "architecture"
    if any(keyword in normalized_text for keyword in ("investigate", "debug", "analyze")):
        return "ambiguous"
    return "implementation"


def _is_destructive_task(task_text: str, constraints: dict[str, Any]) -> bool:
    """Return whether the task involves potentially destructive changes."""
    if constraints.get("destructive_action") is True:
        return True
    normalized_text = task_text.lower()
    return any(
        re.search(rf"\b{re.escape(marker)}\b", normalized_text)
        for marker in DESTRUCTIVE_TASK_MARKERS
    )


def _task_requires_approval(task_text: str, constraints: dict[str, Any]) -> bool:
    """Return whether the task should pause for manual approval."""
    if constraints.get("requires_approval") is True:
        return True
    return _is_destructive_task(task_text, constraints)


def _build_approval_checkpoint(state: OrchestratorState) -> ApprovalCheckpoint:
    """Build approval metadata for the current task, if required."""
    task_text = state.normalized_task_text or state.task.task_text
    if not _task_requires_approval(task_text, state.task.constraints):
        return ApprovalCheckpoint()

    reason = state.task.constraints.get("approval_reason")
    is_destructive = _is_destructive_task(task_text, state.task.constraints)
    if not isinstance(reason, str) or not reason.strip():
        reason = (
            "Task includes a potentially destructive action."
            if is_destructive
            else "Manual approval required for this task."
        )

    task_identifier = state.task.task_id or "pending"
    return ApprovalCheckpoint(
        required=True,
        status="pending",
        approval_type="destructive_action" if is_destructive else "manual_approval",
        reason=reason,
        resume_token=f"approval-{task_identifier}",
    )


def _route_after_check_approval(state_input: OrchestratorState) -> str:
    """Route either to the approval interrupt or straight to dispatch."""
    state = _ensure_state(state_input)
    return "await_approval" if state.approval.required else "dispatch_job"


def _coerce_approval_decision(resume_value: Any) -> bool:
    """Normalize LangGraph resume payloads into a boolean approval decision."""
    if isinstance(resume_value, bool):
        return resume_value

    if isinstance(resume_value, dict):
        val = resume_value.get("approved")
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "yes", "y", "1", "approve", "approved")
        return False

    if isinstance(resume_value, str):
        return resume_value.lower() in ("true", "yes", "y", "1", "approve", "approved")

    return False


def _route_after_await_approval(state_input: OrchestratorState) -> str:
    """Continue to dispatch only when the destructive action was approved."""
    state = _ensure_state(state_input)
    return "dispatch_job" if state.approval.status == "approved" else "summarize_result"


def _build_worker_request(state: OrchestratorState) -> WorkerRequest:
    """Build the typed worker request from orchestrator state."""
    return WorkerRequest(
        session_id=state.session.session_id if state.session is not None else None,
        repo_url=state.task.repo_url,
        branch=state.task.branch,
        task_text=state.normalized_task_text or state.task.task_text,
        memory_context=state.memory.model_dump(),
        constraints=dict(state.task.constraints),
        budget=dict(state.task.budget),
    )


def _default_worker_result_provider(request: WorkerRequest) -> WorkerResult:
    """Return a fake successful worker result for the skeleton happy path."""
    return WorkerResult(
        status="success",
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="persist_memory",
        summary=f"Fake worker completed: {request.task_text}",
    )


class _DefaultFakeWorker(Worker):
    """Fallback worker used until a real provider-specific adapter exists."""

    async def run(self, request: WorkerRequest) -> WorkerResult:
        return _default_worker_result_provider(request)


def _configured_workers(
    worker: Worker | None = None,
    gemini_worker: Worker | None = None,
) -> dict[str, Worker]:
    """Return the workers that are actually wired into the graph."""
    result: dict[str, Worker] = {"codex": worker or _DefaultFakeWorker()}
    if gemini_worker is not None:
        result["gemini"] = gemini_worker
    return result


def _unconfigured_worker_result(worker_type: str | None) -> WorkerResult:
    """Return a structured error when routing selects an unavailable worker."""
    configured_workers = ", ".join(sorted(_configured_workers()))
    selected_worker = worker_type or "unknown"
    return WorkerResult(
        status="error",
        summary=(
            f"No worker is configured for route '{selected_worker}'. "
            f"Configured workers: {configured_workers}."
        ),
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="configure_requested_worker",
    )


def ingest_task(state_input: OrchestratorState) -> dict[str, Any]:
    """Normalize the incoming task text before classification."""
    state = _ensure_state(state_input)
    normalized_task_text = state.task.task_text.strip()
    return {
        "current_step": "ingest_task",
        "normalized_task_text": normalized_task_text,
        "progress_updates": _progress_update(state, "task ingested"),
        "timeline_events": _timeline_event(
            state,
            TimelineEventType.TASK_INGESTED,
            message="Task text normalized.",
        ),
    }


def classify_task(state_input: OrchestratorState) -> dict[str, Any]:
    """Classify the task into a coarse workflow category."""
    state = _ensure_state(state_input)
    task_text = state.normalized_task_text or state.task.task_text
    task_kind = _classify_task_kind(task_text)
    return {
        "current_step": "classify_task",
        "task_kind": task_kind,
        "progress_updates": _progress_update(state, f"task classified as {task_kind}"),
        "timeline_events": _timeline_event(
            state,
            TimelineEventType.TASK_CLASSIFIED,
            message=f"Task classified as {task_kind}.",
            payload={"task_kind": task_kind},
        ),
    }


def load_memory(state_input: OrchestratorState) -> dict[str, Any]:
    """Preserve the current memory context for the skeleton graph."""
    state = _ensure_state(state_input)
    return {
        "current_step": "load_memory",
        "memory": state.memory.model_dump(),
        "progress_updates": _progress_update(state, "memory context loaded"),
        "timeline_events": _timeline_event(
            state,
            TimelineEventType.MEMORY_LOADED,
            message=(
                f"Loaded {len(state.memory.personal)} personal and "
                f"{len(state.memory.project)} project memory entries."
            ),
        ),
    }


def _route_by_preference(
    preferred: WorkerType,
    fallback: WorkerType,
    reason: str,
    available_workers: frozenset[str],
) -> RouteDecision:
    """Pick the preferred worker when available, or the fallback with an explicit reason.

    - preferred available  → reason (e.g. 'high_stakes_refactor')
    - fallback available   → 'preferred_unavailable'  (task runs on the fallback)
    - neither available    → 'runtime_unavailable'    (dispatch will fail explicitly)
    """
    if preferred in available_workers:
        return RouteDecision(
            chosen_worker=preferred,
            route_reason=reason,
            override_applied=False,
        )
    if fallback in available_workers:
        return RouteDecision(
            chosen_worker=fallback,
            route_reason="preferred_unavailable",
            override_applied=False,
        )
    # Neither available - keep the preferred intent; dispatch will fail explicitly.
    return RouteDecision(
        chosen_worker=preferred,
        route_reason="runtime_unavailable",
        override_applied=False,
    )


def _compute_route_decision(
    state: OrchestratorState,
    available_workers: frozenset[str],
) -> RouteDecision:
    """Apply T-071 routing heuristics and T-072 manual override in priority order."""

    # T-072: manual override — honor when the requested runtime is available;
    # fail explicitly otherwise so state never silently claims a worker that isn't present.
    if state.task.worker_override is not None:
        worker_override = state.task.worker_override
        if worker_override in available_workers:
            return RouteDecision(
                chosen_worker=worker_override,
                route_reason="manual_override",
                override_applied=True,
            )
        logger.warning(
            "Manual override requested unavailable worker; routing will fail at dispatch",
            extra={"worker": worker_override, "available": sorted(available_workers)},
        )
        return RouteDecision(
            chosen_worker=worker_override,
            route_reason="runtime_unavailable",
            override_applied=True,
        )

    # T-071: heuristic 1 — escalate to an alternate worker after prior failure.
    if state.attempt_count > 0 and state.dispatch.worker_type is not None:
        prior_worker: WorkerType = state.dispatch.worker_type
        if state.verification is not None and state.verification.status == "failed":
            escalation_reason: str | None = "verifier_failed_previous_run"
        elif state.result is not None and state.result.status != "success":
            escalation_reason = "previous_worker_failed"
        else:
            escalation_reason = None

        if escalation_reason is not None:
            # TODO: generalise alternate selection when the worker pool grows beyond two.
            alternate: WorkerType = "gemini" if prior_worker != "gemini" else "codex"
            if alternate in available_workers:
                logger.info(
                    "Routing to alternate worker due to prior failure",
                    extra={
                        "prior_worker": prior_worker,
                        "alternate_worker": alternate,
                        "reason": escalation_reason,
                    },
                )
                return RouteDecision(
                    chosen_worker=alternate,
                    route_reason=escalation_reason,
                    override_applied=False,
                )
            # Alternate unavailable — fail explicitly rather than blind retry of the failed worker.
            logger.warning(
                "Escalation requires alternate worker but it is unavailable; failing explicitly",
                extra={"prior_worker": prior_worker, "alternate_worker": alternate},
            )
            return RouteDecision(
                chosen_worker=alternate,
                route_reason="runtime_unavailable",
                override_applied=False,
            )

    # T-071: heuristic 2 — explicit budget preference.
    budget = state.task.budget
    if budget.get("prefer_high_quality"):
        return _route_by_preference("gemini", "codex", "budget_preference", available_workers)
    if budget.get("prefer_low_cost"):
        return _route_by_preference("codex", "gemini", "budget_preference", available_workers)

    # T-071: heuristic 3 — task shape.
    task_kind = state.task_kind
    if task_kind == "architecture":
        return _route_by_preference("gemini", "codex", "high_stakes_refactor", available_workers)
    if task_kind == "ambiguous":
        return _route_by_preference("gemini", "codex", "ambiguous_task", available_workers)
    return _route_by_preference("codex", "gemini", "cheap_mechanical_change", available_workers)


def build_choose_worker_node(
    available_workers: frozenset[str],
) -> Callable[[OrchestratorState], dict[str, Any]]:
    """Create the choose-worker node bound to the given set of available workers."""

    def choose_worker_node(state_input: OrchestratorState) -> dict[str, Any]:
        state = _ensure_state(state_input)
        route = _compute_route_decision(state, available_workers)
        return {
            "current_step": "choose_worker",
            "route": route.model_dump(),
            "progress_updates": _progress_update(
                state,
                f"worker selected: {route.chosen_worker} (reason: {route.route_reason})",
            ),
            "timeline_events": _timeline_event(
                state,
                TimelineEventType.WORKER_SELECTED,
                message=f"Worker selected: {route.chosen_worker}",
                payload=route.model_dump(),
            ),
        }

    return choose_worker_node


def choose_worker(state_input: OrchestratorState) -> dict[str, Any]:
    """Apply routing heuristics; treats all known workers as available.

    Use build_choose_worker_node() when the graph knows which workers are wired in.
    """
    state = _ensure_state(state_input)
    route = _compute_route_decision(state, frozenset({"codex", "gemini"}))
    return {
        "current_step": "choose_worker",
        "route": route.model_dump(),
        "progress_updates": _progress_update(
            state,
            f"worker selected: {route.chosen_worker} (reason: {route.route_reason})",
        ),
    }


def check_approval(state_input: OrchestratorState) -> dict[str, Any]:
    """Persist approval metadata before any destructive action is dispatched."""
    state = _ensure_state(state_input)
    approval = _build_approval_checkpoint(state)
    progress_message = "approval requested" if approval.required else "approval not required"
    return {
        "current_step": "check_approval",
        "approval": approval.model_dump(),
        "progress_updates": _progress_update(state, progress_message),
        "timeline_events": _timeline_event(
            state,
            TimelineEventType.APPROVAL_REQUESTED,
            message=f"Approval requested: {approval.reason}"
            if approval.required
            else "Approval not required.",
            payload=approval.model_dump() if approval.required else None,
        ),
    }


def await_approval(state_input: OrchestratorState) -> dict[str, Any]:
    """Pause the graph until a destructive action is approved or rejected."""
    state = _ensure_state(state_input)
    approval = state.approval
    if not approval.required:
        return {
            "current_step": "await_approval",
            "approval": approval.model_dump(),
        }

    task_text = state.normalized_task_text or state.task.task_text
    approved = _coerce_approval_decision(
        interrupt(
            {
                "approval_type": approval.approval_type,
                "reason": approval.reason,
                "resume_token": approval.resume_token,
                "task_text": task_text,
                "chosen_worker": state.route.chosen_worker,
            }
        )
    )

    updated_approval = approval.model_copy(
        update={"status": "approved" if approved else "rejected"},
    )
    progress_message = "approval granted" if approved else "approval rejected"
    response: dict[str, Any] = {
        "current_step": "await_approval",
        "approval": updated_approval.model_dump(),
        "progress_updates": _progress_update(state, progress_message),
    }
    if not approved:
        response["result"] = WorkerResult(
            status="failure",
            summary="Task halted because the requested destructive action was not approved.",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="await_manual_follow_up",
        ).model_dump()
        response["timeline_events"] = _timeline_event(
            state,
            TimelineEventType.APPROVAL_REJECTED,
            message="Task expansion rejected.",
        )
    else:
        response["timeline_events"] = _timeline_event(
            state,
            TimelineEventType.APPROVAL_GRANTED,
            message="Task expansion approved.",
        )
    return response


def dispatch_job(state_input: OrchestratorState) -> dict[str, Any]:
    """Record the chosen worker before awaiting execution."""
    state = _ensure_state(state_input)
    worker_type = state.route.chosen_worker
    assert worker_type is not None, "choose_worker must set route.chosen_worker before dispatch."
    dispatch = WorkerDispatch(
        worker_type=worker_type,
    )
    return {
        "current_step": "dispatch_job",
        "attempt_count": state.attempt_count + 1,
        "dispatch": dispatch.model_dump(),
        "progress_updates": _progress_update(state, "worker dispatched"),
        "timeline_events": _timeline_event(
            state,
            TimelineEventType.WORKER_DISPATCHED,
            message=f"Dispatched attempt {state.attempt_count + 1} to {worker_type}.",
            payload={"attempt_count": state.attempt_count + 1, "worker_type": worker_type},
        ),
    }


def build_await_result_node(
    worker: Worker | None = None,
    gemini_worker: Worker | None = None,
) -> Callable[[OrchestratorState], Awaitable[dict[str, Any]]]:
    """Create the await-result node around the workers wired into the graph."""
    configured_workers = _configured_workers(worker, gemini_worker)

    async def await_result(state_input: OrchestratorState) -> dict[str, Any]:
        state = _ensure_state(state_input)
        worker_type = state.dispatch.worker_type or state.route.chosen_worker
        bound_worker = configured_workers.get(worker_type or "")
        if bound_worker is None:
            result = _unconfigured_worker_result(worker_type)
            progress_updates = _progress_update(
                state,
                f"worker unavailable: {worker_type or 'unknown'}",
            )
        else:
            request = _build_worker_request(state)
            result, progress_message = await _await_worker_with_timeout(
                bound_worker,
                request,
                worker_type=worker_type or "unknown",
                session_id=request.session_id,
                timeout_seconds=_resolve_orchestrator_timeout_seconds(state),
            )
            progress_updates = _progress_update(state, progress_message)
        return {
            "current_step": "await_result",
            "result": result.model_dump(),
            "progress_updates": progress_updates,
            "timeline_events": _timeline_event(
                state,
                (
                    TimelineEventType.WORKER_COMPLETED
                    if result.status == "success"
                    else TimelineEventType.WORKER_FAILED
                    if result.status == "failure"
                    else TimelineEventType.WORKER_ERROR
                ),
                message=result.summary or progress_message,
                payload={"status": result.status},
            ),
        }

    return await_result


def _route_after_await_result(state_input: OrchestratorState) -> str:
    """Route from await_result either to verify_result or await_permission_escalation."""
    state = _ensure_state(state_input)
    if state.result is not None and state.result.next_action_hint == "request_higher_permission":
        return "await_permission_escalation"
    return "verify_result"


def await_permission_escalation(state_input: OrchestratorState) -> dict[str, Any]:
    """Pause the graph to request higher tool permissions from the caller."""
    state = _ensure_state(state_input)
    if not state.result or state.result.next_action_hint != "request_higher_permission":
        return {"current_step": "await_permission_escalation"}

    task_text = state.normalized_task_text or state.task.task_text
    requested_permission = state.result.requested_permission
    if not requested_permission:
        logger.error(
            "Worker requested higher permission but 'requested_permission' is missing.",
            extra={"session_id": state.session.session_id if state.session else None},
        )
        failed_result = state.result.model_copy(
            update={
                "status": "error",
                "summary": "Worker requested higher permission but did not specify which one.",
                "next_action_hint": "inspect_worker_configuration",
            }
        )
        return {
            "current_step": "await_permission_escalation",
            "result": failed_result.model_dump(),
            "progress_updates": _progress_update(
                state, "permission request failed: missing permission name"
            ),
            "timeline_events": _timeline_event(
                state,
                TimelineEventType.WORKER_ERROR,
                message="Worker requested higher permission but did not specify which one.",
            ),
        }
    reason = state.result.summary or f"Worker requested higher permission: {requested_permission}"

    approved = _coerce_approval_decision(
        interrupt(
            {
                "approval_type": "permission_escalation",
                "reason": reason,
                "resume_token": f"permission-{state.task.task_id or 'pending'}",
                "task_text": task_text,
                "chosen_worker": state.route.chosen_worker,
                "requested_permission": requested_permission,
            }
        )
    )

    if approved:
        new_constraints = dict(state.task.constraints)
        new_constraints["granted_permission"] = requested_permission
        updated_task = state.task.model_copy(update={"constraints": new_constraints})

        return {
            "current_step": "await_permission_escalation",
            "task": updated_task.model_dump(),
            "result": None,
            "progress_updates": _progress_update(
                state, f"permission '{requested_permission}' granted"
            ),
            "timeline_events": _timeline_event(
                state,
                TimelineEventType.APPROVAL_GRANTED,
                message=f"Permission '{requested_permission}' granted.",
                payload={"granted_permission": requested_permission},
            ),
        }
    else:
        failed_result = state.result.model_copy(
            update={
                "summary": (
                    f"Permission escalation to '{requested_permission}' "
                    "was rejected. Run halted."
                ),
                "next_action_hint": "await_manual_follow_up",
            }
        )
        return {
            "current_step": "await_permission_escalation",
            "result": failed_result.model_dump(),
            "progress_updates": _progress_update(
                state, f"permission '{requested_permission}' rejected"
            ),
            "timeline_events": _timeline_event(
                state,
                TimelineEventType.APPROVAL_REJECTED,
                message=f"Permission '{requested_permission}' rejected.",
                payload={"requested_permission": requested_permission},
            ),
        }


def _route_after_await_permission_escalation(state_input: OrchestratorState) -> str:
    """Route back to dispatch if approved, else verify failure through verification."""
    state = _ensure_state(state_input)
    if state.result is None:
        return "dispatch_job"
    return "verify_result"


def verify_result(state_input: OrchestratorState) -> dict[str, Any]:
    """Perform deterministic checks on the worker output before summarization."""
    state = _ensure_state(state_input)
    if state.result is None:
        return {
            "current_step": "verify_result",
            "progress_updates": _progress_update(state, "verification skipped: no result"),
        }

    items: list[VerificationReportItem] = []

    # 1. Worker Status
    items.append(
        VerificationReportItem(
            label="worker_status",
            status="passed" if state.result.status == "success" else "failed",
            message=f"Worker reported status: {state.result.status}",
        )
    )

    # 2. Test Results
    failed_tests = [t for t in state.result.test_results if t.status in ("failed", "error")]
    status: Literal["passed", "failed", "warning"] = "warning"
    if state.result.test_results:
        status = "failed" if failed_tests else "passed"
        msg = f"{len(failed_tests)} failed" if failed_tests else "All tests passed"
    else:
        status = "warning"
        msg = "No test results reported"
    items.append(
        VerificationReportItem(
            label="test_results",
            status=status,
            message=msg,
        )
    )

    # 3. File Changes
    if state.result.status == "success" and not state.result.files_changed:
        items.append(
            VerificationReportItem(
                label="file_changes",
                status="warning",
                message="Worker reported success but no files were changed.",
            )
        )
    elif state.result.status != "success" and state.result.files_changed:
        items.append(
            VerificationReportItem(
                label="file_changes",
                status="warning",
                message=(
                    f"Worker reported {state.result.status} "
                    f"but changed {len(state.result.files_changed)} files."
                ),
            )
        )
    else:
        items.append(
            VerificationReportItem(
                label="file_changes",
                status="passed",
                message=f"{len(state.result.files_changed)} files changed.",
            )
        )

    # 4. Command Audit
    failed_commands = [c for c in state.result.commands_run if c.exit_code != 0]
    if failed_commands:
        items.append(
            VerificationReportItem(
                label="command_audit",
                status="warning",
                message=f"{len(failed_commands)} commands exited with non-zero status.",
            )
        )
    else:
        items.append(
            VerificationReportItem(
                label="command_audit",
                status="passed",
                message=f"All {len(state.result.commands_run)} commands exited successfully.",
            )
        )

    # Calculate overall status
    report_status: Literal["passed", "failed", "warning"]
    if any(i.status == "failed" for i in items):
        report_status = "failed"
    elif any(i.status == "warning" for i in items):
        report_status = "warning"
    else:
        report_status = "passed"

    report = VerificationReport(
        status=report_status,
        summary=f"Verification {report_status}: {len(items)} checks run.",
        items=items,
    )

    events_with_start = _timeline_event(state, TimelineEventType.VERIFICATION_STARTED)
    return {
        "current_step": "verify_result",
        "verification": report.model_dump(),
        "progress_updates": _progress_update(state, f"verification {report_status}"),
        "timeline_events": _timeline_event(
            state.model_copy(update={"timeline_events": events_with_start}),
            TimelineEventType.VERIFICATION_COMPLETED,
            message=report.summary,
            payload=report.model_dump(),
        ),
    }


def summarize_result(state_input: OrchestratorState) -> dict[str, Any]:
    """Ensure the worker result has a human-readable summary."""
    state = _ensure_state(state_input)
    if state.result is None:
        result = WorkerResult(
            status="error",
            summary="Worker did not return a result.",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
        )
    elif state.result.summary is None:
        worker_name = state.dispatch.worker_type
        assert worker_name is not None, "dispatch_job must set dispatch.worker_type before summary."
        result = state.result.model_copy(
            update={"summary": f"{worker_name} finished with status {state.result.status}"},
        )
    else:
        result = state.result

    # Extract session state update (T-062)
    session_state_update = SessionStateUpdate(
        active_goal=state.normalized_task_text or state.task.task_text,
        files_touched=result.files_changed,
        # TODO: extract decisions_made and identified_risks from result.summary or a dedicated field
    )

    return {
        "current_step": "summarize_result",
        "result": result.model_dump(),
        "session_state_update": session_state_update.model_dump(),
        "progress_updates": _progress_update(state, "result summarized and session state updated"),
        "timeline_events": _timeline_event(
            state,
            TimelineEventType.TASK_COMPLETED
            if result.status == "success"
            else TimelineEventType.TASK_FAILED,
            message=result.summary,
            payload={"status": result.status},
        ),
    }


def persist_memory(state_input: OrchestratorState) -> dict[str, Any]:
    """Terminate the happy path without yet writing memory anywhere."""
    state = _ensure_state(state_input)
    # Placeholder for skeptical memory update
    return {
        "current_step": "persist_memory",
        "memory_to_persist": [entry.model_dump() for entry in state.memory_to_persist],
        "progress_updates": _progress_update(state, "memory persistence queued"),
    }


def build_orchestrator_graph(
    *,
    worker: Worker | None = None,
    gemini_worker: Worker | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    interrupt_before: Literal["*"] | list[str] | None = None,
    interrupt_after: Literal["*"] | list[str] | None = None,
) -> Any:
    """Build and compile the linear LangGraph happy-path skeleton."""
    builder = StateGraph(OrchestratorState)
    builder.add_node("ingest_task", RunnableLambda(ingest_task))
    builder.add_node("classify_task", RunnableLambda(classify_task))
    builder.add_node("load_memory", RunnableLambda(load_memory))
    available_workers: frozenset[str] = frozenset(_configured_workers(worker, gemini_worker).keys())
    builder.add_node("choose_worker", RunnableLambda(build_choose_worker_node(available_workers)))
    builder.add_node("check_approval", RunnableLambda(check_approval))
    builder.add_node("await_approval", RunnableLambda(await_approval))
    builder.add_node("dispatch_job", RunnableLambda(dispatch_job))
    builder.add_node(
        "await_result",
        RunnableLambda(build_await_result_node(worker, gemini_worker)),
    )
    builder.add_node("await_permission_escalation", RunnableLambda(await_permission_escalation))
    builder.add_node("verify_result", RunnableLambda(verify_result))
    builder.add_node("summarize_result", RunnableLambda(summarize_result))
    builder.add_node("persist_memory", RunnableLambda(persist_memory))
    builder.add_edge(START, "ingest_task")
    builder.add_edge("ingest_task", "classify_task")
    builder.add_edge("classify_task", "load_memory")
    builder.add_edge("load_memory", "choose_worker")
    builder.add_edge("choose_worker", "check_approval")
    builder.add_conditional_edges(
        "check_approval",
        RunnableLambda(_route_after_check_approval),
        {
            "await_approval": "await_approval",
            "dispatch_job": "dispatch_job",
        },
    )
    builder.add_conditional_edges(
        "await_approval",
        RunnableLambda(_route_after_await_approval),
        {
            "dispatch_job": "dispatch_job",
            "summarize_result": "summarize_result",
        },
    )
    builder.add_edge("dispatch_job", "await_result")
    builder.add_conditional_edges(
        "await_result",
        RunnableLambda(_route_after_await_result),
        {
            "await_permission_escalation": "await_permission_escalation",
            "verify_result": "verify_result",
        },
    )
    builder.add_conditional_edges(
        "await_permission_escalation",
        RunnableLambda(_route_after_await_permission_escalation),
        {
            "dispatch_job": "dispatch_job",
            "verify_result": "verify_result",
        },
    )
    builder.add_edge("verify_result", "summarize_result")
    builder.add_edge("summarize_result", "persist_memory")
    builder.add_edge("persist_memory", END)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
        interrupt_after=interrupt_after,
    )
