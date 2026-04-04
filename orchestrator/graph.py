"""LangGraph workflow skeleton for the orchestrator happy path."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, Literal

from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from orchestrator.state import (
    ApprovalCheckpoint,
    OrchestratorState,
    RouteDecision,
    WorkerDispatch,
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
        worker_task.cancel()
        with suppress(asyncio.CancelledError, asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(worker_task, timeout=0)
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
        worker_task.cancel()
        with suppress(asyncio.CancelledError, asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(worker_task, timeout=0)
        return _cancelled_worker_result(), "worker execution cancelled"
    return result, "worker result received"


def _ensure_state(state: OrchestratorState | dict[str, Any]) -> OrchestratorState:
    """Normalize raw graph input into the typed orchestrator state."""
    if isinstance(state, OrchestratorState):
        return state
    return OrchestratorState.model_validate(state)


def _progress_update(state: OrchestratorState, message: str) -> list[str]:
    """Append a progress message while preserving prior updates."""
    return [*state.progress_updates, message]


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


def _configured_workers(worker: Worker | None = None) -> dict[str, Worker]:
    """Return the workers that are actually wired into the graph."""
    return {"codex": worker or _DefaultFakeWorker()}


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
    }


def load_memory(state_input: OrchestratorState) -> dict[str, Any]:
    """Preserve the current memory context for the skeleton graph."""
    state = _ensure_state(state_input)
    return {
        "current_step": "load_memory",
        "memory": state.memory.model_dump(),
        "progress_updates": _progress_update(state, "memory context loaded"),
    }


def choose_worker(state_input: OrchestratorState) -> dict[str, Any]:
    """Apply a minimal routing heuristic for the happy path graph."""
    state = _ensure_state(state_input)

    if state.task.worker_override is not None:
        route = RouteDecision(
            chosen_worker=state.task.worker_override,
            route_reason="manual_override",
            override_applied=True,
        )
    elif state.task_kind in {"architecture", "ambiguous"}:
        route = RouteDecision(
            chosen_worker="claude",
            route_reason="complex_reasoning_default",
            override_applied=False,
        )
    else:
        route = RouteDecision(
            chosen_worker="codex",
            route_reason="implementation_default",
            override_applied=False,
        )

    return {
        "current_step": "choose_worker",
        "route": route.model_dump(),
        "progress_updates": _progress_update(state, f"worker selected: {route.chosen_worker}"),
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
        "dispatch": dispatch.model_dump(),
        "progress_updates": _progress_update(state, "worker dispatched"),
    }


def build_await_result_node(
    worker: Worker | None = None,
) -> Callable[[OrchestratorState], Awaitable[dict[str, Any]]]:
    """Create the await-result node around the workers wired into the graph."""
    configured_workers = _configured_workers(worker)

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
        }

    return await_result


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

    return {
        "current_step": "summarize_result",
        "result": result.model_dump(),
        "progress_updates": _progress_update(state, "result summarized"),
    }


def persist_memory(state_input: OrchestratorState) -> dict[str, Any]:
    """Terminate the happy path without yet writing memory anywhere."""
    state = _ensure_state(state_input)
    return {
        "current_step": "persist_memory",
        "memory_to_persist": [entry.model_dump() for entry in state.memory_to_persist],
        "progress_updates": _progress_update(state, "memory persistence queued"),
    }


def build_orchestrator_graph(
    *,
    worker: Worker | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    interrupt_before: Literal["*"] | list[str] | None = None,
    interrupt_after: Literal["*"] | list[str] | None = None,
) -> Any:
    """Build and compile the linear LangGraph happy-path skeleton."""
    builder = StateGraph(OrchestratorState)
    builder.add_node("ingest_task", RunnableLambda(ingest_task))
    builder.add_node("classify_task", RunnableLambda(classify_task))
    builder.add_node("load_memory", RunnableLambda(load_memory))
    builder.add_node("choose_worker", RunnableLambda(choose_worker))
    builder.add_node("check_approval", RunnableLambda(check_approval))
    builder.add_node("await_approval", RunnableLambda(await_approval))
    builder.add_node("dispatch_job", RunnableLambda(dispatch_job))
    builder.add_node(
        "await_result",
        RunnableLambda(build_await_result_node(worker)),
    )
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
    builder.add_edge("await_result", "summarize_result")
    builder.add_edge("summarize_result", "persist_memory")
    builder.add_edge("persist_memory", END)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
        interrupt_after=interrupt_after,
    )
