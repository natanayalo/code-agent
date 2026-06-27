"""Interaction, approval, and cancellation helpers for task execution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from db.base import utc_now
from db.enums import (
    HumanInteractionStatus,
    HumanInteractionType,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerType,
)
from orchestrator.execution_serialization import _approval_constraints_payload
from orchestrator.execution_types import (
    ApprovalDecisionResult,
    InteractionInboxCard,
    InteractionResponse,
    TaskSnapshot,
)
from orchestrator.state import compute_interaction_content_hash
from repositories import (
    HumanInteractionRepository,
    TaskRepository,
    TaskTimelineRepository,
    WorkerRunRepository,
    session_scope,
)


def list_pending_interactions(self: Any) -> list[InteractionInboxCard]:
    """Retrieve all pending interactions across all tasks, enriched for the inbox."""
    from orchestrator.execution_snapshot_service import _map_human_interaction_snapshot

    with session_scope(self.session_factory) as session:
        repo = HumanInteractionRepository(session)
        rows = repo.list_pending_with_task_context()

        cards = []
        for interaction_row, task_row in rows:
            interaction_snapshot = _map_human_interaction_snapshot(interaction_row)
            cards.append(
                InteractionInboxCard(
                    interaction=interaction_snapshot,
                    task_id=task_row.id,
                    task_text=task_row.task_text,
                    status=task_row.status.value if task_row.status else "unknown",
                    repo_url=task_row.repo_url,
                    branch=task_row.branch,
                    priority=task_row.priority,
                )
            )
        return cards


def record_interaction_response(
    self: Any,
    task_id: str,
    interaction_id: str,
    response: InteractionResponse,
) -> TaskSnapshot | None:
    """Apply an operator response to a pending interaction and trigger task resumption."""
    with session_scope(self.session_factory) as session:
        task_repo = TaskRepository(session)
        interaction_repo = HumanInteractionRepository(session)
        timeline_repo = TaskTimelineRepository(session)

        task = task_repo.get(task_id)
        if not task:
            return None

        interaction, applied = interaction_repo.record_response(
            interaction_id=interaction_id,
            task_id=task_id,
            response_data=response.response_data,
            status=response.status,
        )
        if interaction is None:
            return None

        if applied and interaction.status == HumanInteractionStatus.RESOLVED:
            content_hash = compute_interaction_content_hash(
                interaction.interaction_type,
                interaction.summary,
                interaction.data,
            )
            constraints = dict(task.constraints or {})
            interactions = dict(constraints.get("interactions") or {})
            interactions[content_hash] = {
                "status": "resolved",
                "response_data": response.response_data,
                "interaction_id": interaction.id,
                "interaction_type": interaction.interaction_type,
                "summary": interaction.summary,
                "data": dict(interaction.data or {}) if interaction.data is not None else {},
            }
            constraints["interactions"] = interactions

            if interaction.interaction_type == HumanInteractionType.PERMISSION:
                constraints["requires_approval"] = False
                constraints["approval"] = {
                    "status": "approved",
                    "source": "orchestrator",
                    "reason": f"Permission granted via interaction {interaction.id}",
                    "granted_at": utc_now().isoformat(),
                }

            task.constraints = constraints
            task.next_attempt_at = utc_now()
            task.status = TaskStatus.PENDING
            event_type = (
                TimelineEventType.APPROVAL_GRANTED
                if interaction.interaction_type == HumanInteractionType.PERMISSION
                else TimelineEventType.TASK_SPEC_AND_ROUTE_GENERATED
            )
            timeline_repo.create_next_for_attempt(
                task_id=task_id,
                attempt_number=task.attempt_count,
                event_type=event_type,
                message=f"Interaction '{interaction.interaction_type}' resolved by operator.",
                payload={
                    "interaction_id": interaction.id,
                    "response_data": response.response_data,
                },
            )

        session.flush()
        return self.get_task(task_id)


def _validate_approval_state(
    self: Any,
    task_id: str,
    approval_state: dict[str, Any] | None,
    approved: bool,
) -> ApprovalDecisionResult | None:
    if approval_state is None:
        return ApprovalDecisionResult(
            status="not_waiting",
            detail="Task is not currently awaiting a manual approval decision.",
        )

    current_status = str(approval_state.get("status") or "").strip().lower()
    requested_status = "approved" if approved else "rejected"
    if current_status in {"approved", "rejected"}:
        if current_status == requested_status:
            return ApprovalDecisionResult(
                status="already_applied",
                task_snapshot=self.get_task(task_id),
            )
        return ApprovalDecisionResult(
            status="conflict",
            detail=(
                "Task approval decision already recorded as "
                f"'{current_status}' and cannot be changed."
            ),
        )
    if current_status != "pending":
        return ApprovalDecisionResult(
            status="not_waiting",
            detail="Task is not currently awaiting a manual approval decision.",
        )
    return None


def _handle_approval_rejection(
    task: Any,
    worker_run_repo: Any,
    decided_at: Any,
) -> None:
    task.status = TaskStatus.FAILED
    task.next_attempt_at = None
    task.last_error = "Manual approval rejected via API decision endpoint."
    worker_type = task.chosen_worker or task.worker_override or WorkerType.CODEX
    worker_run_repo.create(
        task_id=task.id,
        session_id=task.session_id,
        worker_type=worker_type,
        workspace_id=None,
        started_at=decided_at,
        finished_at=decided_at,
        status=WorkerRunStatus.FAILURE,
        summary="Manual approval rejected via API decision endpoint; task remains failed.",
        commands_run=[],
        files_changed_count=0,
        files_changed=[],
        artifact_index=[],
    )


def apply_task_approval_decision(
    self: Any,
    *,
    task_id: str,
    approved: bool,
) -> ApprovalDecisionResult:
    """Apply an idempotent approval decision for a paused task."""
    decided_at = utc_now()
    with session_scope(self.session_factory) as session:
        task_repo = TaskRepository(session)
        worker_run_repo = WorkerRunRepository(session)

        task = task_repo.get(task_id)
        if task is None:
            return ApprovalDecisionResult(
                status="not_found",
                detail=f"Task '{task_id}' was not found.",
            )

        constraints = dict(task.constraints or {})
        approval_state_raw = constraints.get("approval")
        approval_state = (
            dict(approval_state_raw) if isinstance(approval_state_raw, Mapping) else None
        )
        validation_error = _validate_approval_state(self, task_id, approval_state, approved)
        if validation_error:
            return validation_error

        # We know approval_state is not None after validation
        assert approval_state is not None
        approval_type = str(approval_state.get("approval_type") or "").strip() or None
        reason = str(approval_state.get("reason") or "").strip() or None
        resume_token = str(approval_state.get("resume_token") or "").strip() or None
        requested_status = "approved" if approved else "rejected"
        constraints["approval"] = _approval_constraints_payload(
            status=requested_status,
            approval_type=approval_type,
            reason=reason,
            resume_token=resume_token,
            updated_at=decided_at,
            source="api",
            approved=approved,
        )

        if approved:
            constraints["requires_approval"] = False
            task.status = TaskStatus.PENDING
            task.next_attempt_at = decided_at
            task.last_error = None
        else:
            _handle_approval_rejection(task, worker_run_repo, decided_at)

        task.constraints = constraints
        task.lease_owner = None
        task.lease_expires_at = None
        session.flush()

    snapshot = self.get_task(task_id)
    if snapshot is None:
        return ApprovalDecisionResult(
            status="not_found",
            detail=f"Task '{task_id}' was not found after applying decision.",
        )
    return ApprovalDecisionResult(status="applied", task_snapshot=snapshot)


def cancel_task(self: Any, *, task_id: str) -> TaskSnapshot | None:
    """Terminally cancel a task and record the lifecycle event."""
    with session_scope(self.session_factory) as session:
        task_repo = TaskRepository(session)
        timeline_repo = TaskTimelineRepository(session)

        task, was_cancelled = task_repo.cancel(task_id=task_id)
        if task is None:
            return None
        if was_cancelled:
            timeline_repo.create_next_for_attempt(
                task_id=task_id,
                attempt_number=task.attempt_count,
                event_type=TimelineEventType.TASK_CANCELLED,
                message="Task was cancelled by operator.",
            )
    return self.get_task(task_id)
