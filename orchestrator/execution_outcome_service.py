"""Execution outcome persistence helpers for task execution."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, cast

from db.enums import ArtifactType, ProposalStatus, TaskStatus, WorkerType
from orchestrator.execution_policy import (
    _task_status_from_result,
    _worker_run_status_from_result,
    _worker_type_for_persistence,
)
from orchestrator.execution_serialization import (
    _approval_constraints_payload,
    _artifact_type_for_persistence,
    _review_result_artifact_entry,
    _serialize_verification_report,
    _workspace_id_from_artifacts,
)
from orchestrator.state import OrchestratorState
from repositories import (
    ArtifactRepository,
    HumanInteractionRepository,
    ProposalRepository,
    SessionStateRepository,
    TaskRepository,
    TaskTimelineRepository,
    WorkerRunRepository,
    session_scope,
)

logger = logging.getLogger("orchestrator.execution")


def _apply_approval_constraints(task: Any, state: OrchestratorState, finished_at: datetime) -> None:
    approval = state.approval
    if not approval.required:
        return
    approval_status = approval.status
    if approval_status in {"pending", "approved", "rejected"}:
        constraints = dict(task.constraints or {})
        constraints["approval"] = _approval_constraints_payload(
            status=approval_status,
            approval_type=approval.approval_type,
            reason=approval.reason,
            resume_token=approval.resume_token,
            updated_at=finished_at,
            source="orchestrator",
            approved=(
                True
                if approval_status == "approved"
                else False
                if approval_status == "rejected"
                else None
            ),
        )
        task.constraints = constraints


def _build_artifact_index(
    state: OrchestratorState,
    artifacts: list[Any],
) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    result = state.result
    artifact_index = [artifact.model_dump(mode="json") for artifact in artifacts]
    review_sources = (
        (
            result.review_result if result is not None else None,
            ArtifactType.REVIEW_RESULT.value,
        ),
        (state.review, ArtifactType.INDEPENDENT_REVIEW_RESULT.value),
    )
    review_artifact_entries: list[tuple[str, dict[str, Any]]] = []
    for review_payload, review_artifact_type in review_sources:
        review_entry = _review_result_artifact_entry(
            review_payload,
            artifact_type=review_artifact_type,
        )
        if review_entry is None:
            continue
        artifact_index.append(review_entry)
        review_artifact_entries.append((review_artifact_type, review_entry))
    return artifact_index, review_artifact_entries


def _create_worker_run(
    worker_run_repo: WorkerRunRepository,
    task_id: str,
    state: OrchestratorState,
    artifacts: list[Any],
    artifact_index: list[dict[str, Any]],
    started_at: datetime,
    finished_at: datetime,
    retention_expires_at: datetime | None,
) -> Any:
    result = state.result
    worker_type = _worker_type_for_persistence(state)
    return worker_run_repo.create(
        task_id=task_id,
        session_id=state.session.session_id if state.session is not None else None,
        worker_type=worker_type,
        workspace_id=_workspace_id_from_artifacts(artifacts),
        started_at=started_at,
        finished_at=finished_at,
        status=_worker_run_status_from_result(state),
        summary=result.summary if result is not None else "Worker did not return a result.",
        requested_permission=result.requested_permission if result is not None else None,
        budget_usage=result.budget_usage if result is not None else None,
        verifier_outcome=_serialize_verification_report(state.verification)
        or ({"failure_kind": result.failure_kind} if result and result.failure_kind else None),
        commands_run=[
            command.model_dump(mode="json")
            for command in (result.commands_run if result is not None else [])
        ],
        files_changed_count=len(result.files_changed) if result is not None else 0,
        files_changed=result.files_changed if result is not None else [],
        artifact_index=artifact_index,
        retention_expires_at=retention_expires_at,
        worker_profile=state.route.chosen_profile,
        runtime_mode=state.route.runtime_mode,
    )


def _persist_timeline_events(
    session: Any,
    task_id: str,
    state: OrchestratorState,
) -> None:
    persisted_count = state.timeline_persisted_count
    current_attempt_events = []
    for event in reversed(state.timeline_events):
        if event.attempt_number != state.attempt_count:
            break
        current_attempt_events.append(event)
    current_attempt_events.reverse()
    new_events = [
        event for event in current_attempt_events if event.sequence_number >= persisted_count
    ]
    if new_events:
        timeline_repo = TaskTimelineRepository(session)
        timeline_repo.create_batch(
            task_id=task_id,
            events=[event.model_dump() for event in new_events],
        )


def _persist_artifacts_for_run(
    artifact_repo: ArtifactRepository,
    worker_run_id: str,
    artifacts: list[Any],
    review_artifact_entries: list[tuple[str, dict[str, Any]]],
) -> None:
    for artifact in artifacts:
        artifact_type = _artifact_type_for_persistence(artifact)
        if artifact_type is None:
            continue
        artifact_repo.create(
            run_id=worker_run_id,
            artifact_type=artifact_type,
            name=artifact.name,
            uri=artifact.uri,
        )
    for review_artifact_type, review_entry in review_artifact_entries:
        artifact_repo.create(
            run_id=worker_run_id,
            artifact_type=review_artifact_type,
            name=review_entry["name"],
            uri=review_entry["uri"],
            artifact_metadata=review_entry["artifact_metadata"],
        )


def _should_create_scout_proposal(state: OrchestratorState) -> bool:
    result = state.result
    return (
        state.task_spec is not None
        and state.task_spec.task_type == "scout"
        and result is not None
        and result.status == "success"
    )


def _persist_scout_proposal_if_needed(
    proposal_repo: ProposalRepository,
    *,
    task: Any,
    state: OrchestratorState,
    artifacts: list[Any],
    worker_run_id: str,
) -> None:
    if not _should_create_scout_proposal(state):
        return

    existing_proposals = proposal_repo.list_proposals(task_id=task.id, limit=1)
    if existing_proposals:
        return

    assert state.result is not None
    proposal = proposal_repo.create_proposal(
        session_id=task.session_id,
        task_id=task.id,
        title=f"Scout Output for Task {task.id}",
        summary=state.result.summary or "Scout task completed without summary.",
        status=ProposalStatus.PENDING_REVIEW,
        metadata_payload={
            "source": "scout",
            "task_id": task.id,
            "worker_run_id": worker_run_id,
            "files_changed": state.result.files_changed,
            "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
            "budget_usage": state.result.budget_usage,
            "diff_text": state.result.diff_text,
            "json_payload": state.result.json_payload,
        },
    )
    logger.info(
        "Persisted scout proposal",
        extra={"task_id": task.id, "proposal_id": proposal.id, "worker_run_id": worker_run_id},
    )


def _update_task_route_and_spec(
    task: Any,
    state: OrchestratorState,
    interaction_repo: HumanInteractionRepository,
) -> None:
    if state.route.chosen_worker is not None and state.route.route_reason is not None:
        task.chosen_worker = cast(WorkerType, state.route.chosen_worker)
        task.chosen_profile = state.route.chosen_profile
        task.runtime_mode = state.route.runtime_mode
        task.route_reason = state.route.route_reason

    if state.task_spec is not None:
        task.task_spec = state.task_spec.model_dump(mode="json")
    if isinstance(task.task_spec, Mapping):
        interaction_repo.sync_task_spec_flags(
            task_id=task.id, task_spec=cast(dict[str, Any], task.task_spec)
        )


def _persist_execution_outcome(
    self: Any,
    *,
    task_id: str,
    state: OrchestratorState,
    started_at: datetime,
    finished_at: datetime,
    force_task_status: TaskStatus | None = None,
) -> None:
    """Persist route, task status, worker-run metadata, and artifacts."""
    logger.info(
        "Persisting execution outcome",
        extra={
            "task_id": task_id,
            "approval_required": state.approval.required,
            "approval_status": state.approval.status,
            "timeline_count": len(state.timeline_events),
        },
    )
    retention_expires_at = (
        finished_at + timedelta(seconds=self.retention_seconds)
        if self.retention_seconds is not None
        else None
    )
    with session_scope(self.session_factory) as session:
        task_repo = TaskRepository(session)
        interaction_repo = HumanInteractionRepository(session)
        worker_run_repo = WorkerRunRepository(session)
        artifact_repo = ArtifactRepository(session)

        task = task_repo.get(task_id)
        if task is None:
            raise RuntimeError(f"Task '{task_id}' disappeared while persisting execution.")

        _update_task_route_and_spec(task, state, interaction_repo)

        task.status = force_task_status or _task_status_from_result(state)

        _apply_approval_constraints(task, state, finished_at)

        result = state.result
        artifacts = result.artifacts if result is not None else []
        artifact_index, review_artifact_entries = _build_artifact_index(state, artifacts)

        worker_run = _create_worker_run(
            worker_run_repo=worker_run_repo,
            task_id=task_id,
            state=state,
            artifacts=artifacts,
            artifact_index=artifact_index,
            started_at=started_at,
            finished_at=finished_at,
            retention_expires_at=retention_expires_at,
        )

        _persist_timeline_events(session, task_id, state)

        if state.session is not None and state.session_state_update is not None:
            session_state_repo = SessionStateRepository(session)
            session_state_repo.upsert(
                session_id=state.session.session_id,
                **state.session_state_update.model_dump(exclude_none=True),
            )

        _persist_artifacts_for_run(
            artifact_repo=artifact_repo,
            worker_run_id=worker_run.id,
            artifacts=artifacts,
            review_artifact_entries=review_artifact_entries,
        )

        _persist_scout_proposal_if_needed(
            ProposalRepository(session),
            task=task,
            state=state,
            artifacts=artifacts,
            worker_run_id=worker_run.id,
        )

    self._prune_retained_runs(now=finished_at)
