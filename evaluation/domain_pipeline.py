"""Evaluation-only invocation of retained orchestration domain callables."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.graph import (
    build_await_result_node,
    build_decompose_task_node,
    build_generate_task_spec_and_route_node,
    build_load_memory_node,
    build_persist_memory_node,
    build_review_result_node,
    check_approval,
    dispatch_job,
    load_memory,
    persist_memory,
    summarize_result,
)
from orchestrator.nodes.delivery import build_deliver_result_node
from orchestrator.nodes.ingestion import (
    classify_task,
    ingest_task,
    load_repo_profile_node,
    plan_task,
)
from orchestrator.nodes.utils import _available_workers
from orchestrator.nodes.verification import build_verify_result_node
from orchestrator.state import OrchestratorState
from workers import Worker, WorkerResult

_DomainNode = Callable[[OrchestratorState], dict[str, Any] | Awaitable[dict[str, Any]]]


class DomainEvaluationPipeline:
    """Run evaluation cases without introducing a production lifecycle engine."""

    def __init__(
        self,
        *,
        worker: Worker,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        available_workers = frozenset(_available_workers(worker))
        memory_node = (
            build_load_memory_node(session_factory) if session_factory is not None else load_memory
        )
        persist_node = (
            build_persist_memory_node(session_factory)
            if session_factory is not None
            else persist_memory
        )
        self._classify_nodes: tuple[_DomainNode, ...] = (
            ingest_task,
            classify_task,
            plan_task,
            load_repo_profile_node,
            build_generate_task_spec_and_route_node(available_workers=available_workers),
            check_approval,
        )
        self._execution_nodes: tuple[_DomainNode, ...] = (
            build_decompose_task_node(session_factory),
            memory_node,
            dispatch_job,
            build_await_result_node(worker, session_factory=session_factory),
            build_verify_result_node(worker=worker),
            build_review_result_node(worker),
            build_deliver_result_node(worker),
            summarize_result,
            persist_node,
        )

    @staticmethod
    def _merge_updates(state: dict[str, Any], updates: dict[str, Any]) -> None:
        additive_fields = {
            "timeline_events",
            "progress_updates",
            "friction_reports",
            "memory_to_persist",
            "errors",
            "scout_phase_results",
        }
        for key, value in updates.items():
            if value is None:
                continue
            if key in additive_fields:
                state[key] = [*(state.get(key) or []), *value]
            else:
                state[key] = value

    async def _invoke_node(self, node: _DomainNode, state: dict[str, Any]) -> None:
        result = node(OrchestratorState.model_validate(state))
        if inspect.isawaitable(result):
            result = await result
        self._merge_updates(state, result)

    @staticmethod
    def _operator_gate_result(state: OrchestratorState) -> WorkerResult | None:
        policy_errors = [error for error in state.errors if error.startswith("task_spec_policy:")]
        clarification_required = bool(state.task_spec and state.task_spec.requires_clarification)
        if not (policy_errors or clarification_required or state.approval.required):
            return None
        reason = (
            policy_errors[0]
            if policy_errors
            else "Task requires clarification before evaluation can continue."
            if clarification_required
            else state.approval.reason or "Task requires approval."
        )
        interaction_kind = (
            "approval"
            if state.approval.required
            else "clarification"
            if clarification_required
            else "input"
        )
        return WorkerResult(
            status="failure",
            failure_kind="interaction",
            summary=f"Evaluation paused for operator {interaction_kind}. {reason}",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="await_manual_follow_up",
        )

    async def run(self, inputs: dict[str, Any]) -> OrchestratorState:
        """Invoke the same domain callables used by Temporal activities."""
        state = OrchestratorState.model_validate(inputs).model_dump(mode="python")
        for node in self._classify_nodes:
            await self._invoke_node(node, state)

        gate_result = self._operator_gate_result(OrchestratorState.model_validate(state))
        if gate_result is not None:
            self._merge_updates(state, {"result": gate_result.model_dump(mode="python")})
            return OrchestratorState.model_validate(state)

        for node in self._execution_nodes:
            await self._invoke_node(node, state)
        return OrchestratorState.model_validate(state)
