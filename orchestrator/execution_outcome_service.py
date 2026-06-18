"""Execution outcome persistence helpers for task execution."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, cast

from pydantic import ValidationError

from db.enums import ArtifactType, ProposalStatus, ProposalType, TaskStatus, WorkerType
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
from orchestrator.improvement_suggestions import build_improvement_suggestion_draft
from orchestrator.nodes.verification_result import VERIFIER_REPAIR_PASSES_USED_CONSTRAINT
from orchestrator.reflection import FrictionReport
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
            "diff_text": getattr(state.result, "diff_text", None),
            "json_payload": getattr(state.result, "json_payload", None),
        },
    )
    logger.info(
        "Persisted scout proposal",
        extra={"task_id": task.id, "proposal_id": proposal.id, "worker_run_id": worker_run_id},
    )


def _persist_friction_proposals_if_needed(
    proposal_repo: ProposalRepository,
    *,
    task_id: str,
    session_id: str,
    task_constraints: dict[str, Any] | None,
    state: OrchestratorState,
    worker_run_id: str,
) -> None:
    all_reports: list[FrictionReport] = list(state.friction_reports)
    result_reports = (
        getattr(state.result, "friction_reports", None) if state.result is not None else None
    )
    if result_reports:
        for rep_dict in result_reports:
            if not isinstance(rep_dict, Mapping):
                logger.warning("Friction report from worker is not a mapping: %r", rep_dict)
                continue
            if not isinstance(rep_dict, dict):
                rep_dict = dict(rep_dict)
            try:
                source = rep_dict.get("source")
                if source not in {"tooling", "orchestrator", "sandbox", "instructions", "other"}:
                    source = "other"
                impact = rep_dict.get("impact")
                if impact not in {"slowed_down", "blocked", "required_workaround", "unknown"}:
                    impact = "unknown"
                desc = rep_dict.get("description")
                if isinstance(desc, str):
                    desc = desc.strip() or None
                elif desc is not None:
                    desc = str(desc).strip() or None
                all_reports.append(
                    FrictionReport(
                        task_id=task_id,
                        worker_run_id=worker_run_id,
                        source=source,  # type: ignore[arg-type]
                        description=desc,
                        impact=impact,  # type: ignore[arg-type]
                        context=rep_dict.get("context"),
                    )
                )
            except (ValidationError, AttributeError) as exc:
                logger.warning("Failed to parse friction report dict from worker: %s", exc)
                logger.debug("Validation details: %s", exc, exc_info=True)

    if not all_reports:
        return

    # User constraint: persist queue only when signal is repeated or repair has been exhausted
    # First attempt is 1, so > 1 implies repeated
    has_retry_context = False
    if state.attempt_count > 1:
        has_retry_context = True
    elif state.route and state.route.route_reason and "retry" in state.route.route_reason.lower():
        has_retry_context = True
    elif (
        isinstance(task_constraints, dict)
        and VERIFIER_REPAIR_PASSES_USED_CONSTRAINT in task_constraints
    ):
        has_retry_context = True

    if not has_retry_context:
        return

    existing_proposals = proposal_repo.list_proposals(task_id=task_id)
    existing_fingerprints = {
        p.metadata_payload.get("fingerprint")
        for p in existing_proposals
        if p.metadata_payload and isinstance(p.metadata_payload, dict)
    }
    failure_kind = getattr(state.result, "failure_kind", None) if state.result else None

    for report in all_reports:
        report = report.model_copy(
            update={
                "task_id": report.task_id or task_id,
                "worker_run_id": report.worker_run_id or worker_run_id,
            }
        )
        draft = build_improvement_suggestion_draft(
            report,
            task_id=task_id,
            attempt_count=state.attempt_count,
            failure_kind=failure_kind,
            retry_context=has_retry_context,
        )

        if draft.fingerprint in existing_fingerprints:
            continue

        proposal = proposal_repo.create_proposal(
            session_id=session_id,
            task_id=task_id,
            title=draft.suggestion.title,
            summary=draft.suggestion.description,
            status=ProposalStatus.PENDING_REVIEW,
            proposal_type=ProposalType.REFLECTION,
            metadata_payload={
                "reflection_kind": "improvement_suggestion",
                "improvement_suggestion": draft.suggestion.model_dump(mode="json"),
                "friction_report": report.model_dump(mode="json"),
                "attempt_count": state.attempt_count,
                "failure_kind": failure_kind,
                "worker_type": state.route.chosen_worker if state.route else None,
                "fingerprint": draft.fingerprint,
            },
        )
        logger.info(
            "Persisted improvement suggestion proposal",
            extra={
                "task_id": task_id,
                "proposal_id": proposal.id,
                "worker_run_id": worker_run_id,
                "title": draft.suggestion.title,
            },
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

        task_id_val = task.id
        session_id_val = task.session_id
        task_constraints_val = task.constraints
        worker_run_id_val = worker_run.id

    try:
        with session_scope(self.session_factory) as prop_session:
            _persist_friction_proposals_if_needed(
                ProposalRepository(prop_session),
                task_id=task_id_val,
                session_id=session_id_val,
                task_constraints=task_constraints_val,
                state=state,
                worker_run_id=worker_run_id_val,
            )
    except Exception as exc:
        logger.warning("Failed to persist friction proposals: %s", exc, exc_info=True)
    self._prune_retained_runs(now=finished_at)
