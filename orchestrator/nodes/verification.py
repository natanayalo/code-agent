"""Verification node implementation."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from apps.observability import (
    SPAN_KIND_CHAIN,
    SPAN_KIND_TOOL,
    set_span_input_output,
    start_optional_span,
)
from db.enums import TimelineEventType
from orchestrator.brain import OrchestratorBrain
from orchestrator.nodes.utils import (
    _available_workers,
    _ensure_state,
)
from orchestrator.nodes.verification_result import (
    VERIFIER_REPAIR_MAX_PASSES_CONSTRAINT,
    VERIFIER_REPAIR_PASSES_USED_CONSTRAINT,
    VERIFIER_REPAIR_REQUEST_CONSTRAINT,
    verify_result,
)
from orchestrator.state import (
    OrchestratorState,
    is_task_read_only,
)
from orchestrator.verification import (
    resolve_verification_commands,
    run_deterministic_verification,
    run_independent_verifier,
)
from workers import Worker

__all__ = [
    "VERIFIER_REPAIR_MAX_PASSES_CONSTRAINT",
    "VERIFIER_REPAIR_PASSES_USED_CONSTRAINT",
    "VERIFIER_REPAIR_REQUEST_CONSTRAINT",
    "verify_result",
]

logger = logging.getLogger(__name__)


def _check_short_circuit_verification(state: OrchestratorState) -> bool:
    if state.result is None:
        logger.warning(
            "Skipping verification node: no worker result available",
            extra={"task_id": state.task.task_id},
        )
        return True

    if state.task_spec is not None and state.task_spec.task_type == "scout":
        logger.info(
            "Short-circuiting verification: scout task produces no code changes to verify",
            extra={"task_id": state.task.task_id},
        )
        return True

    worker_failed = state.result.status != "success"
    tests_failed = any(t.status in ("failed", "error") for t in state.result.test_results)

    if worker_failed or tests_failed:
        logger.info(
            "Short-circuiting verification due to worker or test failure",
            extra={
                "worker_status": state.result.status,
                "failed_tests": len(
                    [t for t in state.result.test_results if t.status in ("failed", "error")]
                ),
            },
        )
        return True
    return False


async def _run_deterministic_step(
    state: OrchestratorState, available_workers: dict
) -> tuple[tuple[Literal["passed", "failed", "warning"], str], dict[str, Any] | None, list[str]]:
    verification_commands = resolve_verification_commands(state)

    if not verification_commands:
        logger.info(
            "Skipping deterministic verification: no commands provided by TaskSpec",
            extra={"task_id": state.task.task_id},
        )
        return ("passed", "No explicit verification commands defined."), None, verification_commands

    with start_optional_span(
        tracer_name="orchestrator.graph",
        span_name="orchestrator.node.verify_result.deterministic",
        attributes={"openinference.span.kind": SPAN_KIND_TOOL},
        task_id=state.task.task_id,
        session_id=state.session.session_id if state.session else None,
        attempt=state.attempt_count,
        task_kind=state.task_kind,
        route_reason=state.route.route_reason if state.route else None,
        verification_summary=state.verification.summary if state.verification else None,
    ):
        (
            deterministic_verifier_status,
            deterministic_verifier_summary,
            deterministic_verifier_metadata,
        ) = await run_deterministic_verification(
            state,
            worker_factory=available_workers,
        )
        deterministic_verifier_outcome = (
            deterministic_verifier_status,
            deterministic_verifier_summary,
        )
        set_span_input_output(
            input_data=verification_commands,
            output_data={
                "outcome": deterministic_verifier_outcome,
                "metadata": deterministic_verifier_metadata,
            },
        )
        return (
            deterministic_verifier_outcome,
            deterministic_verifier_metadata,
            verification_commands,
        )


async def _run_independent_step(
    state: OrchestratorState,
    available_workers: dict,
    enable_independent_verifier: bool,
    deterministic_verifier_outcome: tuple,
) -> tuple[tuple[Literal["passed", "failed", "warning"], str] | None, str | None]:
    if not enable_independent_verifier or deterministic_verifier_outcome[0] == "failed":
        return None, None

    is_read_only = is_task_read_only(state)
    no_files_changed = not (state.result and state.result.files_changed)

    if is_read_only or no_files_changed:
        reason = "read-only task" if is_read_only else "no files changed"
        logger.info(
            "Skipping independent verifier (%s)",
            reason,
            extra={"task_id": state.task.task_id},
        )
        return None, "skip_read_only_or_no_changes"

    with start_optional_span(
        tracer_name="orchestrator.graph",
        span_name="orchestrator.node.verify_result.independent",
        attributes={"openinference.span.kind": SPAN_KIND_TOOL},
        task_id=state.task.task_id,
        session_id=state.session.session_id if state.session else None,
        attempt=state.attempt_count,
        task_kind=state.task_kind,
        route_reason=state.route.route_reason if state.route else None,
        verification_summary=state.verification.summary if state.verification else None,
    ):
        status, summary, reason_code = await run_independent_verifier(
            state,
            worker_factory=available_workers,
        )
        independent_verifier_outcome = (status, summary)
        set_span_input_output(
            input_data=state.result.summary if state.result else None,
            output_data={
                "outcome": independent_verifier_outcome,
                "reason_code": reason_code,
            },
        )
        return independent_verifier_outcome, reason_code


def _build_extra_events(
    verification_commands: list[str], deterministic_verifier_metadata: dict[str, Any] | None
) -> list[tuple[TimelineEventType, str | None, dict[str, Any] | None]]:
    extra_events: list[tuple[TimelineEventType, str | None, dict[str, Any] | None]] = []
    if not verification_commands:
        extra_events.append(
            (
                TimelineEventType.VERIFICATION_SKIPPED,
                "Deterministic verification skipped: no commands provided by TaskSpec.",
                None,
            )
        )
    elif deterministic_verifier_metadata is not None:
        extra_events.append(
            (
                TimelineEventType.VERIFICATION_SKIPPED
                if deterministic_verifier_metadata.get("skip_reason_code")
                else TimelineEventType.VERIFICATION_STARTED,
                (
                    "Deterministic verification skipped due to placeholder-only commands."
                    if deterministic_verifier_metadata.get("skip_reason_code")
                    else "Deterministic verification filtered placeholder commands."
                ),
                {"deterministic_verification": deterministic_verifier_metadata},
            )
        )
    return extra_events


def build_verify_result_node(
    *,
    enable_independent_verifier: bool = False,
    worker: Worker | None = None,
    gemini_worker: Worker | None = None,
    openrouter_worker: Worker | None = None,
    shell_worker: Worker | None = None,
    orchestrator_brain: OrchestratorBrain | None = None,
) -> Callable[[OrchestratorState], Awaitable[dict[str, Any]]]:
    """Factory for the verification node."""

    available_workers = _available_workers(
        worker, gemini_worker, openrouter_worker, shell_worker=shell_worker
    )

    async def verify_result_node(state_input: OrchestratorState) -> dict[str, Any]:
        state = _ensure_state(state_input)
        with start_optional_span(
            tracer_name="orchestrator.graph",
            span_name="orchestrator.node.verify_result",
            attributes={"openinference.span.kind": SPAN_KIND_CHAIN},
            task_id=state.task.task_id,
            session_id=state.session.session_id if state.session else None,
            attempt=state.attempt_count,
            task_kind=state.task_kind,
            route_reason=state.route.route_reason if state.route else None,
            verification_summary=state.verification.summary if state.verification else None,
        ):
            logger.info(
                "Entering verify_result_node",
                extra={
                    "session_id": state.session.session_id if state.session else None,
                    "task_id": state.task.task_id,
                    "attempt": state.attempt_count,
                },
            )

            if _check_short_circuit_verification(state):
                if state.result is not None:
                    set_span_input_output(
                        input_data=state.result.status, output_data="short-circuited"
                    )
                return verify_result(state)

            (
                deterministic_verifier_outcome,
                deterministic_verifier_metadata,
                verification_commands,
            ) = await _run_deterministic_step(state, available_workers)

            (
                independent_verifier_outcome,
                independent_verifier_reason_code,
            ) = await _run_independent_step(
                state,
                available_workers,
                enable_independent_verifier,
                deterministic_verifier_outcome,
            )

            extra_events = _build_extra_events(
                verification_commands, deterministic_verifier_metadata
            )

            return verify_result(
                state,
                enable_independent_verifier=enable_independent_verifier,
                deterministic_verifier_outcome=deterministic_verifier_outcome,
                deterministic_verifier_metadata=deterministic_verifier_metadata,
                independent_verifier_outcome=independent_verifier_outcome,
                independent_verifier_reason_code=independent_verifier_reason_code,
                extra_timeline_events=extra_events,
            )

    return verify_result_node
