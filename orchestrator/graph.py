"""LangGraph workflow skeleton for the orchestrator happy path."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph

from orchestrator.state import OrchestratorState, RouteDecision, WorkerDispatch, WorkerResult

WorkerResultProvider = Callable[[OrchestratorState], WorkerResult]

ORCHESTRATOR_NODE_SEQUENCE = (
    "ingest_task",
    "classify_task",
    "load_memory",
    "choose_worker",
    "dispatch_job",
    "await_result",
    "summarize_result",
    "persist_memory",
)


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


def _default_worker_result_provider(state: OrchestratorState) -> WorkerResult:
    """Return a fake successful worker result for the skeleton happy path."""
    return WorkerResult(
        status="success",
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="persist_memory",
        summary=f"Fake worker completed: {state.task.task_text}",
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


def dispatch_job(state_input: OrchestratorState) -> dict[str, Any]:
    """Create a fake dispatch record for the worker run."""
    state = _ensure_state(state_input)
    task_identifier = state.task.task_id or "pending"
    worker_type = state.route.chosen_worker
    assert worker_type is not None, "choose_worker must set route.chosen_worker before dispatch."
    dispatch = WorkerDispatch(
        run_id=f"run-{task_identifier}",
        worker_type=worker_type,
        workspace_id=f"workspace-{task_identifier}",
    )
    return {
        "current_step": "dispatch_job",
        "dispatch": dispatch.model_dump(),
        "progress_updates": _progress_update(state, "worker dispatched"),
    }


def build_await_result_node(
    worker_result_provider: WorkerResultProvider | None = None,
) -> Callable[[OrchestratorState], dict[str, Any]]:
    """Create the await-result node around a fake or injected worker provider."""
    result_provider = worker_result_provider or _default_worker_result_provider

    def await_result(state_input: OrchestratorState) -> dict[str, Any]:
        state = _ensure_state(state_input)
        result = result_provider(state)
        return {
            "current_step": "await_result",
            "result": result.model_dump(),
            "progress_updates": _progress_update(state, "worker result received"),
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
    worker_result_provider: WorkerResultProvider | None = None,
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
    builder.add_node("dispatch_job", RunnableLambda(dispatch_job))
    builder.add_node(
        "await_result",
        RunnableLambda(build_await_result_node(worker_result_provider)),
    )
    builder.add_node("summarize_result", RunnableLambda(summarize_result))
    builder.add_node("persist_memory", RunnableLambda(persist_memory))
    builder.add_edge(START, "ingest_task")
    builder.add_edge("ingest_task", "classify_task")
    builder.add_edge("classify_task", "load_memory")
    builder.add_edge("load_memory", "choose_worker")
    builder.add_edge("choose_worker", "dispatch_job")
    builder.add_edge("dispatch_job", "await_result")
    builder.add_edge("await_result", "summarize_result")
    builder.add_edge("summarize_result", "persist_memory")
    builder.add_edge("persist_memory", END)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
        interrupt_after=interrupt_after,
    )
