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
from orchestrator.brain import (
    OrchestratorBrain,
    VerificationBrainMergeReport,
    VerificationBrainSuggestion,
)
from orchestrator.nodes.utils import (
    _available_workers,
    _ensure_state,
)
from orchestrator.state import OrchestratorState
from orchestrator.verification import (
    resolve_verification_commands,
    run_deterministic_verification,
    run_independent_verifier,
)
from workers import Worker

logger = logging.getLogger(__name__)


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
    from orchestrator.graph import verify_result  # Temporary import to avoid circular dependency

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
        ):
            logger.info(
                "Entering verify_result_node",
                extra={
                    "session_id": state.session.session_id if state.session else None,
                    "task_id": state.task.task_id,
                    "attempt": state.attempt_count,
                },
            )

            # 1. Immediate deterministic checks (short-circuit if worker already failed)
            if state.result is None:
                logger.warning(
                    "Skipping verification node: no worker result available",
                    extra={"task_id": state.task.task_id},
                )
                return verify_result(state)

            worker_failed = state.result.status != "success"
            tests_failed = any(t.status in ("failed", "error") for t in state.result.test_results)

            if worker_failed or tests_failed:
                logger.info(
                    "Short-circuiting verification due to worker or test failure",
                    extra={
                        "worker_status": state.result.status,
                        "failed_tests": len(
                            [
                                t
                                for t in state.result.test_results
                                if t.status in ("failed", "error")
                            ]
                        ),
                    },
                )
                set_span_input_output(input_data=state.result.status, output_data="short-circuited")
                return verify_result(state)

            deterministic_verifier_outcome: tuple[Literal["passed", "failed", "warning"], str]
            independent_verifier_outcome: (
                tuple[Literal["passed", "failed", "warning"], str] | None
            ) = None
            independent_verifier_reason_code: str | None = None

            # 2. Run explicit verification commands deterministically
            verification_commands = resolve_verification_commands(state)

            if not verification_commands:
                logger.info(
                    "Skipping deterministic verification: no commands provided by TaskSpec",
                    extra={"task_id": state.task.task_id},
                )
                deterministic_verifier_outcome = (
                    "passed",
                    "No explicit verification commands defined.",
                )
            else:
                with start_optional_span(
                    tracer_name="orchestrator.graph",
                    span_name="orchestrator.node.verify_result.deterministic",
                    attributes={"openinference.span.kind": SPAN_KIND_TOOL},
                    task_id=state.task.task_id,
                    session_id=state.session.session_id if state.session else None,
                    attempt=state.attempt_count,
                ):
                    deterministic_verifier_outcome = await run_deterministic_verification(
                        state,
                        worker_factory=available_workers,
                    )
                    set_span_input_output(
                        input_data=verification_commands,
                        output_data=deterministic_verifier_outcome,
                    )

            # 3. Run LLM-based independent verifier if enabled and deterministic checks passed
            if enable_independent_verifier and deterministic_verifier_outcome[0] != "failed":
                with start_optional_span(
                    tracer_name="orchestrator.graph",
                    span_name="orchestrator.node.verify_result.independent",
                    attributes={"openinference.span.kind": SPAN_KIND_TOOL},
                    task_id=state.task.task_id,
                    session_id=state.session.session_id if state.session else None,
                    attempt=state.attempt_count,
                ):
                    independent_verifier_result = await run_independent_verifier(
                        state,
                        worker_factory=available_workers,
                    )
                    (
                        independent_verifier_outcome,
                        independent_verifier_reason_code,
                    ) = (
                        (
                            independent_verifier_result[0],
                            independent_verifier_result[1],
                        ),
                        independent_verifier_result[2],
                    )
                    set_span_input_output(
                        input_data=state.result.summary if state.result else None,
                        output_data={
                            "outcome": independent_verifier_outcome,
                            "reason_code": independent_verifier_reason_code,
                        },
                    )

            verification_brain_suggestion: VerificationBrainSuggestion | None = None
            verification_brain_report: VerificationBrainMergeReport | None = None

            if orchestrator_brain is not None:
                provider_name = type(orchestrator_brain).__name__
                try:
                    with start_optional_span(
                        tracer_name="orchestrator.graph",
                        span_name="orchestrator.node.verify_result.brain",
                        attributes={"openinference.span.kind": SPAN_KIND_TOOL},
                        task_id=state.task.task_id,
                        session_id=state.session.session_id if state.session else None,
                        attempt=state.attempt_count,
                    ):
                        verification_brain_suggestion = (
                            await orchestrator_brain.suggest_verification(
                                state=state,
                                independent_verifier_outcome=independent_verifier_outcome,
                            )
                        )
                        set_span_input_output(
                            input_data=independent_verifier_outcome,
                            output_data=(
                                verification_brain_suggestion.model_dump()
                                if verification_brain_suggestion
                                else None
                            ),
                        )
                except Exception as exc:
                    detail = str(exc).strip()
                    verification_brain_report = VerificationBrainMergeReport(
                        enabled=True,
                        provider=provider_name,
                        error=(f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__),
                    )
                else:
                    if verification_brain_suggestion is None:
                        verification_brain_report = VerificationBrainMergeReport(
                            enabled=True,
                            provider=provider_name,
                            rationale="Brain declined to provide verification guidance.",
                        )
                    else:
                        verification_brain_report = VerificationBrainMergeReport(
                            enabled=True,
                            provider=provider_name,
                            rationale=verification_brain_suggestion.rationale,
                            accept_warning_status=verification_brain_suggestion.accept_warning_status,
                        )

            return verify_result(
                state,
                enable_independent_verifier=enable_independent_verifier,
                deterministic_verifier_outcome=deterministic_verifier_outcome,
                independent_verifier_outcome=independent_verifier_outcome,
                independent_verifier_reason_code=independent_verifier_reason_code,
                verification_brain_suggestion=verification_brain_suggestion,
                verification_brain_report=verification_brain_report,
            )

    return verify_result_node
