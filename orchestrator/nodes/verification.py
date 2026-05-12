"""Verification node implementation."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final, Literal

from apps.observability import (
    SPAN_KIND_CHAIN,
    SPAN_KIND_TOOL,
    set_span_input_output,
    start_optional_span,
)
from db.enums import TimelineEventType
from orchestrator.brain import (
    OrchestratorBrain,
    VerificationBrainMergeReport,
    VerificationBrainSuggestion,
)
from orchestrator.nodes.utils import (
    _available_workers,
    _dedupe_preserving_order,
    _ensure_state,
    _has_meaningful_deliverable,
    _progress_update,
    _requires_deliverable_evidence,
    _timeline_events,
)
from orchestrator.state import (
    OrchestratorState,
    VerificationFailureKind,
    VerificationReport,
    VerificationReportItem,
)
from orchestrator.verification import (
    resolve_verification_commands,
    run_deterministic_verification,
    run_independent_verifier,
)
from tools.numeric import coerce_non_negative_int_like
from workers import Worker

logger = logging.getLogger(__name__)

DEFAULT_INDEPENDENT_VERIFIER_MAX_REPAIR_PASSES = 1
VERIFIER_REPAIR_REQUEST_CONSTRAINT: Final = "independent_verifier_repair_request"
VERIFIER_REPAIR_PASSES_USED_CONSTRAINT: Final = "independent_verifier_repair_passes_used"
VERIFIER_REPAIR_MAX_PASSES_CONSTRAINT: Final = "independent_verifier_max_repair_passes"
_VERIFIER_REPAIRABLE_FAILURE_KINDS: Final = frozenset(
    {
        "test_regression",
        "scope_mismatch",
        "worker_failure",
        "unknown",
    }
)
_VERIFIER_REPAIRABLE_WORKER_FAILURE_KINDS: Final = frozenset(
    {
        "compile",
        "test",
        "tool_runtime",
    }
)


def _resolve_verifier_repair_handoff_budget(state: OrchestratorState) -> tuple[int, int]:
    """Resolve bounded verifier-repair settings from constraints and verifier budget caps."""
    constraints = state.task.constraints if isinstance(state.task.constraints, dict) else {}
    budget = state.task.budget if isinstance(state.task.budget, dict) else {}

    configured_max_passes = coerce_non_negative_int_like(
        constraints.get(VERIFIER_REPAIR_MAX_PASSES_CONSTRAINT)
    )
    max_passes = (
        configured_max_passes
        if configured_max_passes is not None
        else DEFAULT_INDEPENDENT_VERIFIER_MAX_REPAIR_PASSES
    )

    verifier_budget_cap = coerce_non_negative_int_like(budget.get("max_verifier_passes"))
    if verifier_budget_cap is not None:
        max_passes = min(max_passes, verifier_budget_cap)

    used_passes = coerce_non_negative_int_like(
        constraints.get(VERIFIER_REPAIR_PASSES_USED_CONSTRAINT)
    )
    if used_passes is None:
        used_passes = 0
    return max_passes, used_passes


def _cleanup_verifier_repair_handoff_constraints(constraints: Mapping[str, Any]) -> dict[str, Any]:
    """Drop transient verifier-repair task text after a bounded repair attempt."""
    cleaned = dict(constraints)
    cleaned.pop(VERIFIER_REPAIR_REQUEST_CONSTRAINT, None)
    return cleaned


def _build_verifier_repair_task_text(
    state: OrchestratorState,
    report: VerificationReport,
) -> str:
    """Create a focused repair task from failed verification checks."""
    task_text = state.normalized_task_text or state.task.task_text
    failed_checks = [item for item in report.items if item.status == "failed"]
    verification_commands = resolve_verification_commands(state)

    lines = [
        "Apply targeted code fixes for failed verification checks.",
        "Keep changes minimal and inside the original task scope.",
        f"Original task objective: {task_text}",
        "",
        "Failed verification checks:",
    ]

    if failed_checks:
        for index, check in enumerate(failed_checks, start=1):
            message = check.message or "No additional details were reported."
            lines.append(f"{index}. {check.label}: {message}")
    else:
        lines.append(
            "1. Verification failed without per-check details; inspect the latest diff and tests."
        )

    if verification_commands:
        lines.extend(
            [
                "",
                "Re-run these verification commands when applicable:",
                *[f"- {command}" for command in verification_commands],
            ]
        )

    lines.extend(
        [
            "",
            "After applying fixes, run the smallest relevant verification commands and summarize.",
        ]
    )
    return "\n".join(lines)


def _manual_verifier_handoff_summary(
    existing_summary: str | None,
    *,
    used_passes: int,
) -> str:
    """Append a human-readable handoff note once verifier repair attempts are exhausted."""
    attempt_label = "attempt" if used_passes == 1 else "attempts"
    handoff_note = (
        "Verification is still failing after "
        f"{used_passes} bounded repair {attempt_label}; manual follow-up is required."
    )
    if isinstance(existing_summary, str) and existing_summary.strip():
        return f"{existing_summary.rstrip()}\n\n{handoff_note}"
    return handoff_note


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


def verify_result(
    state_input: OrchestratorState,
    *,
    enable_independent_verifier: bool = False,
    deterministic_verifier_outcome: (
        tuple[Literal["passed", "failed", "warning"], str] | None
    ) = None,
    independent_verifier_outcome: tuple[Literal["passed", "failed", "warning"], str] | None = None,
    independent_verifier_reason_code: str | None = None,
    verification_brain_suggestion: VerificationBrainSuggestion | None = None,
    verification_brain_report: VerificationBrainMergeReport | None = None,
    extra_timeline_events: list[tuple[TimelineEventType, str | None, dict[str, Any] | None]]
    | None = None,
) -> dict[str, Any]:
    """Perform deterministic checks on the worker output before summarization."""
    state = _ensure_state(state_input)
    if state.result is None:
        return {
            "current_step": "verify_result",
            "progress_updates": _progress_update(state, "verification skipped: no result"),
        }

    items: list[VerificationReportItem] = []

    # 1. Immediate deterministic checks on previous worker's result
    # Worker Status Check
    if state.result.status == "success":
        items.append(
            VerificationReportItem(
                label="worker_status",
                status="passed",
                message="Worker reported success.",
            )
        )
    else:
        items.append(
            VerificationReportItem(
                label="worker_status",
                status="failed",
                message=state.result.summary or "Worker reported failure without summary.",
            )
        )

    # Test Results Check
    if state.result.test_results:
        failed_tests = [r for r in state.result.test_results if r.status in ("failed", "error")]
        if failed_tests:
            failed_names = [r.name for r in failed_tests]
            items.append(
                VerificationReportItem(
                    label="tests",
                    status="failed",
                    message=f"Deterministic tests failed: {', '.join(failed_names)}",
                )
            )
        else:
            items.append(
                VerificationReportItem(
                    label="tests",
                    status="passed",
                    message=f"{len(state.result.test_results)} tests passed.",
                )
            )
    else:
        items.append(
            VerificationReportItem(
                label="tests",
                status="warning",
                message="No test results reported by worker.",
            )
        )

    # 2. Deterministic Verification Commands (from run_deterministic_verification)
    if deterministic_verifier_outcome is not None:
        status, summary = deterministic_verifier_outcome
        items.append(
            VerificationReportItem(
                label="deterministic_commands",
                status=status,
                message=summary,
            )
        )

    # 3. File Changes
    if state.result.status == "success" and not state.result.files_changed:
        if _requires_deliverable_evidence(state) and not _has_meaningful_deliverable(state):
            items.append(
                VerificationReportItem(
                    label="file_changes",
                    status="failed",
                    message=(
                        "Worker reported success but did not produce a concrete deliverable "
                        "(files/artifacts/diff)."
                    ),
                    reason_code="incomplete_delivery",
                )
            )
        else:
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

    # 5. Post-run lint/format
    post_run_lint_format: dict[str, Any] = {}
    if isinstance(state.result.budget_usage, dict):
        lint_metadata = state.result.budget_usage.get("post_run_lint_format")
        if isinstance(lint_metadata, dict):
            post_run_lint_format = lint_metadata
    if post_run_lint_format.get("ran") is False:
        items.append(
            VerificationReportItem(
                label="post_run_lint_format",
                status="passed",
                message="Post-run lint/format step skipped: no detectable command.",
            )
        )
    else:
        lint_errors = post_run_lint_format.get("errors")
        if isinstance(lint_errors, list) and lint_errors:
            items.append(
                VerificationReportItem(
                    label="post_run_lint_format",
                    status="warning",
                    message=f"Post-run lint/format reported {len(lint_errors)} issue(s).",
                )
            )
        elif post_run_lint_format:
            items.append(
                VerificationReportItem(
                    label="post_run_lint_format",
                    status="passed",
                    message="Post-run lint/format completed without reported issues.",
                )
            )

    # 6. Optional independent verifier execution (T-158)
    if enable_independent_verifier:
        independent_status: Literal["passed", "failed", "warning"]
        independent_summary: str
        if independent_verifier_outcome is None:
            independent_status = "warning"
            independent_summary = (
                "Independent verifier enabled, but no verifier outcome was attached."
            )
        else:
            independent_status, independent_summary = independent_verifier_outcome
        items.append(
            VerificationReportItem(
                label="independent_verifier",
                status=independent_status,
                message=independent_summary,
                reason_code=independent_verifier_reason_code,
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

    brain_report = verification_brain_report
    if verification_brain_suggestion is not None:
        if brain_report is None:
            brain_report = VerificationBrainMergeReport(
                enabled=True,
                accept_warning_status=verification_brain_suggestion.accept_warning_status,
                rationale=verification_brain_suggestion.rationale,
            )
        ignored_fields = list(brain_report.ignored_fields)
        applied = brain_report.applied
        if verification_brain_suggestion.accept_warning_status is True:
            if report_status == "warning":
                report_status = "passed"
                applied = True
            else:
                ignored_fields.append("accept_warning_status")
        brain_report = brain_report.model_copy(
            update={
                "applied": applied,
                "ignored_fields": _dedupe_preserving_order(ignored_fields),
            }
        )

    report_failure_kind: VerificationFailureKind | None = None
    if report_status == "failed":
        failed_labels = {item.label for item in items if item.status == "failed"}
        if "tests" in failed_labels:
            report_failure_kind = "test_regression"
        elif "independent_verifier" in failed_labels:
            report_failure_kind = "test_regression"
        elif "deterministic_commands" in failed_labels:
            report_failure_kind = "test_regression"
        elif "file_changes" in failed_labels:
            file_change_failure = next(
                (
                    item
                    for item in items
                    if item.label == "file_changes" and item.status == "failed"
                ),
                None,
            )
            if (
                file_change_failure is not None
                and file_change_failure.reason_code == "incomplete_delivery"
            ):
                report_failure_kind = "incomplete_delivery"
            else:
                report_failure_kind = "scope_mismatch"
        elif "command_audit" in failed_labels:
            report_failure_kind = "risky_command"
        elif "worker_status" in failed_labels:
            report_failure_kind = "worker_failure"
        else:
            report_failure_kind = "unknown"

    report = VerificationReport(
        status=report_status,
        summary=f"Verification {report_status}: {len(items)} checks run.",
        failure_kind=report_failure_kind,
        items=items,
    )
    if brain_report is not None:
        brain_report = brain_report.model_copy(update={"final_verification_status": report.status})
    progress_message = f"verification {report_status}"
    if (
        brain_report is not None
        and brain_report.applied
        and report.status == "passed"
        and verification_brain_suggestion is not None
        and verification_brain_suggestion.accept_warning_status is True
    ):
        progress_message = "verification passed via brain warning-acceptance hint"
    updated_task: dict[str, Any] | None = None
    updated_result: dict[str, Any] | None = None
    repair_handoff_requested = False
    max_passes, used_passes = _resolve_verifier_repair_handoff_budget(state)
    verifier_repair_request = state.task.constraints.get(VERIFIER_REPAIR_REQUEST_CONSTRAINT)
    had_verifier_repair_request = isinstance(verifier_repair_request, str) and bool(
        verifier_repair_request.strip()
    )
    repairable_worker_failure = (
        report.failure_kind == "worker_failure"
        and state.result.status == "failure"
        and state.result.failure_kind in _VERIFIER_REPAIRABLE_WORKER_FAILURE_KINDS
    )

    if (
        report.status == "failed"
        and report.failure_kind in _VERIFIER_REPAIRABLE_FAILURE_KINDS
        and used_passes < max_passes
        and (state.result.status == "success" or repairable_worker_failure)
    ):
        repair_task_text = _build_verifier_repair_task_text(state, report)
        updated_constraints = dict(state.task.constraints)
        updated_constraints[VERIFIER_REPAIR_REQUEST_CONSTRAINT] = repair_task_text
        updated_constraints[VERIFIER_REPAIR_PASSES_USED_CONSTRAINT] = used_passes + 1
        updated_task = state.task.model_copy(
            update={"constraints": updated_constraints}
        ).model_dump()
        repair_handoff_requested = True
        progress_message = (
            f"verification failed; queued bounded repair handoff ({used_passes + 1}/{max_passes})"
        )
    elif had_verifier_repair_request:
        cleaned_constraints = _cleanup_verifier_repair_handoff_constraints(state.task.constraints)
        if cleaned_constraints != state.task.constraints:
            updated_task = state.task.model_copy(
                update={"constraints": cleaned_constraints}
            ).model_dump()
        if report.status == "failed":
            progress_message = "verification failed after bounded repair attempts"
            updated_result = state.result.model_copy(
                update={
                    "summary": _manual_verifier_handoff_summary(
                        state.result.summary,
                        used_passes=used_passes,
                    ),
                    "next_action_hint": "await_manual_follow_up",
                }
            ).model_dump()
        else:
            progress_message = f"verification {report_status} after bounded repair handoff"

    if (
        report.status == "failed"
        and not repair_handoff_requested
        and state.result.status == "success"
        and updated_result is None
    ):
        updated_result = state.result.model_copy(
            update={
                "status": "failure",
                "failure_kind": "unknown",
                "next_action_hint": "await_manual_follow_up",
            }
        ).model_dump()

    verification_payload = report.model_dump()
    if brain_report is not None:
        verification_payload["brain"] = brain_report.model_dump(mode="json")

    response: dict[str, Any] = {
        "current_step": "verify_result",
        "verification": report.model_dump(),
        "progress_updates": _progress_update(state, progress_message),
        **_timeline_events(
            state,
            (TimelineEventType.VERIFICATION_STARTED, None, None),
            *(extra_timeline_events or []),
            (
                TimelineEventType.VERIFICATION_COMPLETED,
                report.summary,
                verification_payload,
            ),
        ),
    }
    if updated_task is not None:
        response["task"] = updated_task
    if updated_result is not None:
        response["result"] = updated_result
    if repair_handoff_requested:
        response["repair_handoff_requested"] = True
    return response
