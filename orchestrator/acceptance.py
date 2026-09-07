"""Deterministic task acceptance, independent of provider success claims."""

from __future__ import annotations

from db.enums import TimelineEventType
from orchestrator.state import OrchestratorState, VerificationReport
from orchestrator.verification import resolve_verification_commands
from workers.base import FailureKind, WorkerResult


def verification_rejection(state: OrchestratorState) -> tuple[FailureKind, str] | None:
    """Reject unmet required checks while permitting unrelated advisory warnings."""
    report = state.verification
    if isinstance(report, dict):
        report = VerificationReport.model_validate(report)
    commands = resolve_verification_commands(state)
    if report is None:
        if commands or (
            state.task_spec and state.task_spec.delivery_mode in {"branch", "draft_pr"}
        ):
            return "infra_verifier_unavailable", "Required verification report is missing."
        return None
    if report.status == "failed":
        return report.failure_kind or "test_regression", report.summary or "Verification failed."
    items = {item.label: item for item in report.items}
    required = {"independent_verifier"} & items.keys()
    if commands:
        required.add("deterministic_commands")
    for label in sorted(required):
        item = items.get(label)
        if item is None or item.status != "passed":
            kind: FailureKind = (
                "test_regression"
                if item and item.status == "failed"
                else "infra_verifier_unavailable"
            )
            return kind, f"Required verification did not pass: {label}."
    return None


def task_acceptance_rejection(state: OrchestratorState) -> tuple[FailureKind, str] | None:
    """Require current-attempt broker evidence for externally delivered work."""
    if state.result is None:
        return "worker_failure", "Worker result is missing."
    if state.result.status != "success":
        return (
            state.result.failure_kind or "worker_failure",
            state.result.summary or "Worker failed.",
        )
    if rejection := verification_rejection(state):
        return rejection
    mode = state.task_spec.delivery_mode if state.task_spec else "summary"
    if mode not in {"branch", "draft_pr"}:
        return None
    events = [
        event
        for event in state.timeline_events
        if event.attempt_number == state.attempt_count
        and event.event_type
        in {TimelineEventType.DELIVERY_COMPLETED, TimelineEventType.DELIVERY_FAILED}
    ]
    metadata = state.result.delivery_metadata or {}
    branch = metadata.get("branch_name")
    if (
        not events
        or events[-1].event_type != TimelineEventType.DELIVERY_COMPLETED
        or not branch
        or (events[-1].payload or {}).get("branch") != branch
        or (mode == "draft_pr" and not metadata.get("pr_url"))
    ):
        return "incomplete_delivery", f"Required {mode} delivery has no broker-confirmed evidence."
    return None


def enforce_task_acceptance(state: OrchestratorState) -> None:
    """Preserve execution artifacts while making rejected acceptance explicit."""
    if rejection := task_acceptance_rejection(state):
        kind, message = rejection
        if state.result is None:
            state.result = WorkerResult(status="failure", failure_kind=kind, summary=message)
        elif state.result.status == "success":
            summary = f"{state.result.summary or ''}\n\nAcceptance failed: {message}".strip()
            state.result = state.result.model_copy(
                update={
                    "status": "failure",
                    "failure_kind": kind,
                    "summary": summary,
                }
            )
