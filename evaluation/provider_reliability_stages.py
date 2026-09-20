"""Stage inspection and timeline validation for provider reliability extraction."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from db.enums import (
    ArtifactType,
    HumanInteractionType,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
)
from db.models import Task, WorkerRun
from evaluation.provider_reliability_models import VALID_FAILURE_KINDS, MutationMode

TERMINAL_FAILURE_EVENT_TYPES: frozenset[TimelineEventType] = frozenset(
    {
        TimelineEventType.DELIVERY_FAILED,
        TimelineEventType.WORKER_FAILED,
        TimelineEventType.WORKER_ERROR,
        TimelineEventType.INFRA_FAILURE,
        TimelineEventType.TASK_FAILED,
    }
)


def is_profile_compatible_with_mode(profile: str, mode: MutationMode) -> bool:
    """Check if worker profile capabilities match the requested execution mode."""
    is_read_only_profile = profile.endswith("-read-only")
    return is_read_only_profile if mode == "read_only" else not is_read_only_profile


def determine_task_mutation_mode(
    task_spec: dict[str, Any] | None,
    constraints: dict[str, Any] | None,
    profile: str | None,
) -> MutationMode | None:
    """Determine task mutation mode using TaskSpec allowed actions and constraints."""
    if task_spec:
        if task_spec.get("task_type") == "scout":
            return "read_only"
        allowed_actions = task_spec.get("allowed_actions")
        if isinstance(allowed_actions, list) and allowed_actions:
            return "mutation" if "modify_workspace_files" in allowed_actions else "read_only"

    c = constraints or {}
    if c.get("read_only") is True or c.get("task_type") == "scout":
        return "read_only"
    if profile and profile.endswith("-read-only"):
        return "read_only"
    return "mutation"


def verify_timeline_consistency(
    status: TaskStatus,
    timeline_events: list[Any],
    updated_at: datetime | None,
) -> tuple[bool, datetime | None]:
    """Verify terminal event consistency and return terminal timestamp."""
    completed_event = None
    failed_event = None
    cancelled_event = None

    for ev in timeline_events:
        etype = ev.event_type
        if etype == TimelineEventType.TASK_COMPLETED:
            completed_event = ev
        elif etype == TimelineEventType.TASK_FAILED:
            failed_event = ev
        elif etype == TimelineEventType.TASK_CANCELLED:
            cancelled_event = ev

    if cancelled_event is not None:
        return False, None

    if status == TaskStatus.COMPLETED:
        if completed_event is None or failed_event is not None:
            return False, None
        return True, completed_event.created_at or updated_at

    if status == TaskStatus.FAILED:
        if failed_event is None or completed_event is not None:
            return False, None
        return True, failed_event.created_at or updated_at

    return False, None


def check_verification_stage(task: Task, runs: list[WorkerRun]) -> tuple[bool, bool]:
    """Inspect verification applicability and pass/fail outcome independently."""
    spec = task.task_spec or {}
    c = task.constraints or {}
    v_cmds = spec.get("verification_commands") or c.get("verification_commands") or []
    has_v_events = any("verification" in str(e.event_type) for e in task.timeline_events)
    v_skipped = any(
        e.event_type == TimelineEventType.VERIFICATION_SKIPPED for e in task.timeline_events
    )
    v_applicable = bool(v_cmds or has_v_events) and not v_skipped
    if not v_applicable:
        return False, False

    v_completed = [
        e for e in task.timeline_events if e.event_type == TimelineEventType.VERIFICATION_COMPLETED
    ]
    if v_completed:
        last_v = v_completed[-1]
        p = last_v.payload or {}
        if p.get("status") in ("passed", "success"):
            return True, True
        if p.get("status") in ("failed", "failure"):
            return True, False
        if p.get("status") == "warning":
            return True, False

    for r in reversed(runs):
        outcome = r.verifier_outcome or {}
        if outcome.get("status") in ("passed", "success"):
            return True, True
        if outcome.get("status") in ("failed", "failure"):
            return True, False

    return True, False


def _check_review_artifacts(runs: list[WorkerRun]) -> tuple[bool, bool]:
    """Parse persisted ReviewResult artifacts to determine applicability and pass/fail."""
    has_review_artifact = False
    review_passed = False

    for r in runs:
        for entry in r.artifact_index or []:
            atype = entry.get("artifact_type")
            if atype == ArtifactType.INDEPENDENT_REVIEW_RESULT.value:
                meta = entry.get("artifact_metadata", {})
                content = meta.get(atype) if isinstance(meta.get(atype), dict) else None
                if content is None:
                    for v in meta.values():
                        if isinstance(v, dict):
                            content = v
                            break
                if content is None:
                    content = {}

                has_review_artifact = True
                outcome = content.get("outcome")
                findings = content.get("findings")
                if outcome == "no_findings":
                    review_passed = True
                elif outcome == "findings" or bool(findings):
                    review_passed = False
                elif content.get("approved") is True or content.get("status") in (
                    "approved",
                    "passed",
                ):
                    review_passed = True
                elif content.get("approved") is False or content.get("status") in (
                    "rejected",
                    "failed",
                ):
                    review_passed = False

    return has_review_artifact, review_passed


def check_review_stage(task: Task, runs: list[WorkerRun], v_passed: bool) -> tuple[bool, bool]:
    """Inspect review applicability and pass/fail outcome independently."""
    has_review_artifact, review_passed = _check_review_artifacts(runs)
    if not has_review_artifact:
        return False, False
    return True, review_passed


def check_delivery_stage(task: Task, runs: list[WorkerRun]) -> tuple[bool, bool]:
    """Inspect delivery applicability and completion outcome independently."""
    spec = task.task_spec or {}
    deliv_mode = spec.get("delivery_mode")
    has_d_events = any("delivery" in str(e.event_type) for e in task.timeline_events)
    d_applicable = deliv_mode in ("branch", "draft_pr") or has_d_events
    if not d_applicable:
        return False, False

    attempt_num = task.attempt_count
    d_events = [
        e
        for e in task.timeline_events
        if (attempt_num is None or e.attempt_number == attempt_num)
        and e.event_type
        in (TimelineEventType.DELIVERY_COMPLETED, TimelineEventType.DELIVERY_FAILED)
    ]
    if not d_events:
        return True, False

    last_event = d_events[-1]
    if last_event.event_type == TimelineEventType.DELIVERY_COMPLETED:
        return True, True

    return True, False


def check_task_stages(
    task: Task, runs: list[WorkerRun]
) -> tuple[bool, bool, bool, bool, bool, bool, bool, bool]:
    """Inspect dispatch, execution, verification, review, and delivery stage outcomes."""
    dispatched = len(runs) > 0
    exec_success = any(r.status == WorkerRunStatus.SUCCESS for r in runs)
    v_applicable, v_passed = check_verification_stage(task, runs)
    rev_applicable, rev_passed = check_review_stage(task, runs, v_passed)
    d_applicable, d_passed = check_delivery_stage(task, runs)
    return (
        dispatched,
        exec_success,
        v_applicable,
        v_passed,
        rev_applicable,
        rev_passed,
        d_applicable,
        d_passed,
    )


def resolve_failure_kind(task: Task, accepted: bool, runs: list[WorkerRun]) -> str | None:
    """Resolve typed failure cause deterministically from timeline events and worker runs."""
    if accepted:
        return None

    target_attempt = task.attempt_count
    for event in reversed(task.timeline_events or []):
        if target_attempt is not None and event.attempt_number != target_attempt:
            continue
        if event.event_type in TERMINAL_FAILURE_EVENT_TYPES:
            payload = event.payload if isinstance(event.payload, dict) else {}
            fk = payload.get("failure_kind")
            if fk and str(fk) in VALID_FAILURE_KINDS:
                return str(fk)

    for r in reversed(runs):
        outcome = r.verifier_outcome if isinstance(r.verifier_outcome, dict) else {}
        fk = outcome.get("failure_kind")
        if fk and str(fk) in VALID_FAILURE_KINDS:
            return str(fk)

    if task.last_error:
        return "task_error"
    return "unknown"


def extract_task_interaction_metrics(
    task: Task, c: dict[str, Any], terminal_ts: datetime
) -> tuple[int, int, bool, float | None]:
    """Extract clarifications, approvals, override flags, and duration."""
    interactions = task.human_interactions or []
    clarifications = sum(
        1 for i in interactions if i.interaction_type == HumanInteractionType.CLARIFICATION
    )
    appr_types = (
        HumanInteractionType.PERMISSION,
        HumanInteractionType.REVIEW,
        HumanInteractionType.MERGE,
    )
    approvals = sum(1 for i in interactions if i.interaction_type in appr_types)
    has_override = bool(
        task.worker_override
        or c.get("worker_override")
        or c.get("worker_profile_override")
        or task.route_reason in ("manual_override", "manual_profile_override")
    )
    start_ts = task.created_at
    if start_ts and start_ts.tzinfo is None:
        start_ts = start_ts.replace(tzinfo=UTC)
    duration = max(0.0, (terminal_ts - start_ts).total_seconds()) if start_ts else None
    return clarifications, approvals, has_override, duration
