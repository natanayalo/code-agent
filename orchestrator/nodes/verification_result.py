from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final, Literal

from db.enums import TimelineEventType
from orchestrator.nodes.utils import (
    _ensure_state,
    _has_meaningful_deliverable,
    _progress_update,
    _requires_deliverable_evidence,
    _timeline_events,
)
from orchestrator.reflection import FrictionReport
from orchestrator.state import (
    OrchestratorState,
    VerificationFailureKind,
    VerificationReport,
    VerificationReportItem,
    is_task_read_only,
)
from orchestrator.verification import resolve_verification_commands
from tools.numeric import coerce_non_negative_int_like

logger = logging.getLogger(__name__)

DEFAULT_INDEPENDENT_VERIFIER_MAX_REPAIR_PASSES = 1

VERIFIER_REPAIR_REQUEST_CONSTRAINT: Final = "independent_verifier_repair_request"
VERIFIER_REPAIR_PASSES_USED_CONSTRAINT: Final = "independent_verifier_repair_passes_used"
VERIFIER_REPAIR_MAX_PASSES_CONSTRAINT: Final = "independent_verifier_max_repair_passes"
_VERIFIER_REPAIRABLE_FAILURE_KINDS: Final = frozenset(
    {
        "test_regression",
        "scope_mismatch",
        "incomplete_delivery",
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
            (
                "Light investigation is allowed only when directly tied to explaining "
                "a failed verification check."
            ),
            "Do not perform broad repo debugging, unrelated root-cause exploration, or refactors.",
        ]
    )
    return "\n".join(lines)


def _manual_verifier_handoff_summary(
    existing_summary: str | None,
    *,
    used_passes: int,
    json_payload: dict[str, Any] | None = None,
) -> str:
    """Append a human-readable handoff note once verifier repair attempts are exhausted."""
    attempt_label = "attempt" if used_passes == 1 else "attempts"
    handoff_note = (
        "Verification is still failing after "
        f"{used_passes} bounded repair {attempt_label}; manual follow-up is required."
    )

    summary = existing_summary or ""
    if json_payload and "final_message" in json_payload:
        msg = json_payload["final_message"]
        if isinstance(msg, str) and msg.strip():
            if msg.strip() not in summary:
                summary = f"{summary.rstrip()}\n\nWorker message: {msg.strip()}"

    if summary.strip():
        return f"{summary.strip()}\n\n{handoff_note}"
    return handoff_note


def _check_independent_verifier(
    enable_independent_verifier: bool,
    independent_verifier_outcome: tuple[Literal["passed", "failed", "warning"], str] | None,
    independent_verifier_reason_code: str | None,
) -> VerificationReportItem | None:
    if not enable_independent_verifier:
        return None
    if independent_verifier_outcome is None:
        if independent_verifier_reason_code == "skip_read_only_or_no_changes":
            return VerificationReportItem(
                label="independent_verifier",
                status="passed",
                message=(
                    "Independent verifier intentionally skipped (read-only or no files changed)."
                ),
                reason_code=independent_verifier_reason_code,
            )
        return VerificationReportItem(
            label="independent_verifier",
            status="warning",
            message="Independent verifier enabled, but no verifier outcome was attached.",
            reason_code=independent_verifier_reason_code,
        )
    return VerificationReportItem(
        label="independent_verifier",
        status=independent_verifier_outcome[0],
        message=independent_verifier_outcome[1],
        reason_code=independent_verifier_reason_code,
    )


def _check_deterministic_commands(
    state: OrchestratorState,
    deterministic_verifier_outcome: tuple[Literal["passed", "failed", "warning"], str] | None,
    deterministic_verifier_metadata: dict[str, Any] | None,
) -> VerificationReportItem | None:
    if deterministic_verifier_outcome is None:
        return None
    status, summary = deterministic_verifier_outcome
    if status == "failed" and is_task_read_only(state):
        status = "warning"
    return VerificationReportItem(
        label="deterministic_commands",
        status=status,
        message=summary,
        reason_code=(
            deterministic_verifier_metadata.get("skip_reason_code")
            if isinstance(deterministic_verifier_metadata, dict)
            else None
        ),
    )


def _build_verify_response(
    state: OrchestratorState,
    report: VerificationReport,
    deterministic_verifier_metadata: dict[str, Any] | None,
    extra_timeline_events: list[tuple[TimelineEventType, str | None, dict[str, Any] | None]] | None,
    progress_message: str,
    updated_task: dict[str, Any] | None,
    updated_result: dict[str, Any] | None,
    repair_handoff_requested: bool,
) -> dict[str, Any]:
    verification_payload = report.model_dump()
    if deterministic_verifier_metadata is not None:
        verification_payload["deterministic_verification"] = deterministic_verifier_metadata

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

    if report.status == "failed":
        is_infra_error = (
            state.result is not None
            and state.result.status == "failure"
            and state.result.failure_kind in ("provider_error", "timeout", "sandbox_infra")
        )
        if not is_infra_error:
            friction_report = FrictionReport(
                task_id=state.task.task_id,
                worker_run_id=state.dispatch.run_id,
                source="tooling",
                description=f"Verification failed: {report.summary or 'unknown error'}",
                impact="blocked",
                context={
                    "failure_kind": report.failure_kind,
                    "items": [
                        item.model_dump() for item in report.items if item.status == "failed"
                    ],
                    "repair_handoff_requested": repair_handoff_requested,
                },
            )
            response["friction_reports"] = state.friction_reports + [friction_report]

    return response


def _resolve_report_status_and_kind(
    items: list[VerificationReportItem],
) -> tuple[Literal["passed", "failed", "warning"], VerificationFailureKind | None]:
    report_status: Literal["passed", "failed", "warning"]
    if any(i.status == "failed" for i in items):
        report_status = "failed"
    elif any(i.status == "warning" for i in items):
        report_status = "warning"
    else:
        report_status = "passed"

    if report_status != "failed":
        return report_status, None

    failed_labels = {item.label for item in items if item.status == "failed"}
    if (
        "tests" in failed_labels
        or "independent_verifier" in failed_labels
        or "deterministic_commands" in failed_labels
    ):
        return report_status, "test_regression"

    file_change_failure = next(
        (item for item in items if item.label == "file_changes" and item.status == "failed"),
        None,
    )
    if file_change_failure is not None and file_change_failure.reason_code == "incomplete_delivery":
        return report_status, "incomplete_delivery"
    if "file_changes" in failed_labels:
        return report_status, "scope_mismatch"
    if "command_audit" in failed_labels:
        return report_status, "risky_command"
    if "worker_status" in failed_labels:
        return report_status, "worker_failure"
    return report_status, "unknown"


def _check_worker_status(state: OrchestratorState) -> VerificationReportItem:
    result = state.result
    assert result is not None
    if result.status == "success":
        return VerificationReportItem(
            label="worker_status",
            status="passed",
            message="Worker reported success.",
        )
    return VerificationReportItem(
        label="worker_status",
        status="failed",
        message=result.summary or "Worker reported failure without summary.",
    )


def _check_test_results(state: OrchestratorState) -> VerificationReportItem:
    result = state.result
    assert result is not None
    if not result.test_results:
        return VerificationReportItem(
            label="tests",
            status="warning",
            message="No test results reported by worker.",
        )
    failed_tests = [r for r in result.test_results if r.status in ("failed", "error")]
    if failed_tests:
        return VerificationReportItem(
            label="tests",
            status="failed",
            message=f"{len(failed_tests)}/{len(result.test_results)} tests failed.",
        )
    return VerificationReportItem(
        label="tests",
        status="passed",
        message=f"{len(result.test_results)} tests passed.",
    )


def _check_file_changes(state: OrchestratorState) -> VerificationReportItem:
    result = state.result
    is_read_only = is_task_read_only(state)

    if result is None:
        logger.warning("Verification result is None")
        return VerificationReportItem(
            label="verification_error",
            status="failed",
            message="No result received from worker.",
            reason_code="missing_result",
        )

    if is_read_only and result.files_changed:
        return VerificationReportItem(
            label="file_changes",
            status="failed",
            message=(
                f"Worker reported {result.status} and changed {len(result.files_changed)} files, "
                "but task was read-only."
            ),
            reason_code="scope_mismatch",
        )

    if result.status == "success" and not result.files_changed:
        if _requires_deliverable_evidence(state) and not _has_meaningful_deliverable(state):
            return VerificationReportItem(
                label="file_changes",
                status="failed",
                message=(
                    "Worker reported success but did not produce a concrete deliverable "
                    "(files/artifacts/diff)."
                ),
                reason_code="incomplete_delivery",
            )
        else:
            if is_read_only:
                return VerificationReportItem(
                    label="file_changes",
                    status="passed",
                    message=(
                        "Worker reported success with no files changed (expected for read-only)."
                    ),
                )
            return VerificationReportItem(
                label="file_changes",
                status="warning",
                message="Worker reported success but no files were changed.",
            )
    elif result.status != "success" and result.files_changed:
        return VerificationReportItem(
            label="file_changes",
            status="warning",
            message=(
                f"Worker reported {result.status} but changed {len(result.files_changed)} files."
            ),
        )
    else:
        if is_read_only and result.files_changed:
            return VerificationReportItem(
                label="file_changes",
                status="failed",
                message=(
                    f"Worker reported success and changed {len(result.files_changed)} files, "
                    "but task was read-only."
                ),
                reason_code="scope_mismatch",
            )
        return VerificationReportItem(
            label="file_changes",
            status="passed",
            message=f"{len(result.files_changed)} files changed.",
        )


def _check_command_audit(state: OrchestratorState) -> VerificationReportItem:
    result = state.result
    assert result is not None
    if not result.commands_run:
        return VerificationReportItem(
            label="command_audit",
            status="passed",
            message="No commands run.",
        )

    failed_commands = [c for c in result.commands_run if c.exit_code != 0]
    if failed_commands:
        return VerificationReportItem(
            label="command_audit",
            status="warning",
            message=f"{len(failed_commands)}/{len(result.commands_run)} commands returned non-zero exit codes.",  # noqa: E501
        )
    return VerificationReportItem(
        label="command_audit",
        status="passed",
        message=f"All {len(result.commands_run)} commands exited successfully.",
    )


def _check_post_run_lint(state: OrchestratorState) -> VerificationReportItem:
    result = state.result
    assert result is not None
    post_run_lint_format: dict[str, Any] = {}
    if isinstance(result.budget_usage, dict):
        lint_metadata = result.budget_usage.get("post_run_lint_format")
        if isinstance(lint_metadata, dict):
            post_run_lint_format = lint_metadata
    if post_run_lint_format.get("ran") is False:
        return VerificationReportItem(
            label="post_run_lint_format",
            status="passed",
            message="Post-run lint/format step skipped: no detectable command.",
        )
    else:
        lint_errors = post_run_lint_format.get("errors")
        if isinstance(lint_errors, list) and lint_errors:
            return VerificationReportItem(
                label="post_run_lint_format",
                status="warning",
                message=f"Post-run lint/format reported {len(lint_errors)} issue(s).",
            )
        elif post_run_lint_format:
            return VerificationReportItem(
                label="post_run_lint_format",
                status="passed",
                message="Post-run lint/format completed without reported issues.",
            )
        return VerificationReportItem(
            label="post_run_lint_format",
            status="passed",
            message="Post-run lint/format was not run.",
        )


def _handle_repair_handoff(
    state: OrchestratorState, report: VerificationReport
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool, str]:
    result = state.result
    assert result is not None
    progress_message = f"verification {report.status}"
    updated_task: dict[str, Any] | None = None
    updated_result: dict[str, Any] | None = None
    repair_handoff_requested = False
    max_passes, used_passes = _resolve_verifier_repair_handoff_budget(state)
    constraints = state.task.constraints if isinstance(state.task.constraints, dict) else {}
    verifier_repair_request = constraints.get(VERIFIER_REPAIR_REQUEST_CONSTRAINT)
    had_verifier_repair_request = isinstance(verifier_repair_request, str) and bool(
        verifier_repair_request.strip()
    )
    repairable_worker_failure = (
        report.failure_kind == "worker_failure"
        and result.status == "failure"
        and result.failure_kind in _VERIFIER_REPAIRABLE_WORKER_FAILURE_KINDS
    )

    if (
        report.status == "failed"
        and report.failure_kind in _VERIFIER_REPAIRABLE_FAILURE_KINDS
        and used_passes < max_passes
        and (result.status == "success" or repairable_worker_failure)
    ):
        repair_task_text = _build_verifier_repair_task_text(state, report)
        updated_constraints = dict(constraints)
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
        cleaned_constraints = _cleanup_verifier_repair_handoff_constraints(constraints)
        if cleaned_constraints != constraints:
            updated_task = state.task.model_copy(
                update={"constraints": cleaned_constraints}
            ).model_dump()
        if report.status == "failed":
            progress_message = "verification failed after bounded repair attempts"
            updated_result = result.model_copy(
                update={
                    "status": "failure",
                    "failure_kind": result.failure_kind or report.failure_kind or "unknown",
                    "summary": _manual_verifier_handoff_summary(
                        result.summary,
                        used_passes=used_passes,
                        json_payload=result.json_payload,
                    ),
                    "next_action_hint": "await_manual_follow_up",
                }
            ).model_dump()
        else:
            progress_message = f"verification {report.status} after bounded repair handoff"

    if (
        report.status == "failed"
        and not repair_handoff_requested
        and result.status == "success"
        and updated_result is None
    ):
        updated_result = result.model_copy(
            update={
                "status": "failure",
                "failure_kind": report.failure_kind or "unknown",
                "summary": f"{report.summary}\n\n{result.summary or ''}".strip(),
                "next_action_hint": "await_manual_follow_up",
            }
        ).model_dump()

    return updated_task, updated_result, repair_handoff_requested, progress_message


def verify_result(
    state_input: OrchestratorState,
    *,
    enable_independent_verifier: bool = False,
    deterministic_verifier_outcome: (
        tuple[Literal["passed", "failed", "warning"], str] | None
    ) = None,
    deterministic_verifier_metadata: dict[str, Any] | None = None,
    independent_verifier_outcome: tuple[Literal["passed", "failed", "warning"], str] | None = None,
    independent_verifier_reason_code: str | None = None,
    extra_timeline_events: list[tuple[TimelineEventType, str | None, dict[str, Any] | None]]
    | None = None,
    sc_reason: str | None = None,
) -> dict[str, Any]:
    """Perform deterministic checks on the worker output before summarization."""
    state = _ensure_state(state_input)
    if state.result is None:
        return {
            "current_step": "verify_result",
            "progress_updates": _progress_update(state, "verification skipped: no result"),
        }

    items: list[VerificationReportItem] = []

    items.append(_check_worker_status(state))
    items.append(_check_test_results(state))

    # 2. Deterministic Verification Commands (from run_deterministic_verification)
    if det_item := _check_deterministic_commands(
        state, deterministic_verifier_outcome, deterministic_verifier_metadata
    ):
        items.append(det_item)

    # 3. File Changes
    items.append(_check_file_changes(state))

    # 4. Command Audit
    items.append(_check_command_audit(state))

    # 5. Post-run lint/format
    items.append(_check_post_run_lint(state))

    # 6. Optional independent verifier execution (T-158)
    if indep_item := _check_independent_verifier(
        enable_independent_verifier,
        independent_verifier_outcome,
        independent_verifier_reason_code,
    ):
        items.append(indep_item)

    # Calculate overall status
    report_status, report_failure_kind = _resolve_report_status_and_kind(items)

    report = VerificationReport(
        status=report_status,
        summary=f"Verification {report_status}: {len(items)} checks run.",
        failure_kind=report_failure_kind,
        items=items,
    )
    (
        updated_task,
        updated_result,
        repair_handoff_requested,
        progress_message,
    ) = _handle_repair_handoff(state, report)

    return _build_verify_response(
        state,
        report,
        deterministic_verifier_metadata,
        extra_timeline_events,
        sc_reason
        if sc_reason is not None and sc_reason != "worker_or_test_failure"
        else progress_message,
        updated_task,
        updated_result,
        repair_handoff_requested,
    )
