"""Execution outcome persistence helpers for task execution."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, cast, get_args

from db.enums import ArtifactType, ProposalStatus, ProposalType, TaskStatus, WorkerType
from orchestrator.execution_improvement_proposal_service import (
    _persist_friction_proposals_if_needed,
)
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
from orchestrator.scout_proposals import (
    ScoutProposal,
    ScoutProposalValidationError,
    compute_scout_proposal_fingerprint,
    scout_max_proposals_from_constraints,
    validate_scout_proposal_payload,
)
from orchestrator.state import OrchestratorState
from repositories import (
    ArtifactRepository,
    ExecutionPlanRepository,
    HumanInteractionRepository,
    ProposalRepository,
    SessionStateRepository,
    TaskRepository,
    TaskTimelineRepository,
    WorkerRunRepository,
    session_scope,
)

logger = logging.getLogger("orchestrator.execution")

ScoutMode = Literal["repo", "research", "deep"]
ALLOWED_SCOUT_MODES: Final[set[str]] = set(get_args(ScoutMode))


@dataclass(frozen=True)
class _PersistedExecutionOutcome:
    """Identifiers needed for post-transaction proposal persistence."""

    task_id: str
    session_id: str
    task_constraints: dict[str, Any] | None
    worker_run_id: str


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
        files_changed_count=len(result.files_changed)
        if result is not None and result.files_changed
        else 0,
        files_changed=result.files_changed if result is not None else None,
        artifact_index=artifact_index,
        runtime_manifest=state.dispatch.runtime_manifest if state.dispatch else None,
        retention_expires_at=retention_expires_at,
        worker_profile=(state.dispatch.worker_profile if state.dispatch else None)
        or (state.route.chosen_profile if state.route else None),
        runtime_mode=(state.dispatch.runtime_mode if state.dispatch else None)
        or (state.route.runtime_mode if state.route else None),
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


def _merge_scout_phase_result(
    pr_res: Any,
    phase: str,
    scout_phase_metadata: list[dict[str, Any]],
    summary_parts: list[str],
    files_changed: list[str],
    all_artifacts: list[Any],
    budget_usage: dict[str, Any],
) -> None:
    """Merge an individual scout phase result into the aggregated payload."""
    if pr_res is None:
        logger.warning("Scout phase result is None for phase: %s", phase)
        summary = "No summary available."
        scout_phase_metadata.append({"phase": phase, "summary": summary})
        summary_parts.append(f"{phase.capitalize()} phase: {summary}")
        return

    summary = (pr_res.summary or "").strip() or "No summary available."
    scout_phase_metadata.append({"phase": phase, "summary": summary})
    summary_parts.append(f"{phase.capitalize()} phase: {summary}")

    if pr_res.files_changed:
        for f in pr_res.files_changed:
            if f not in files_changed:
                files_changed.append(f)
    if pr_res.artifacts:
        all_artifacts.extend([artifact.model_dump(mode="json") for artifact in pr_res.artifacts])

    for k, v in (pr_res.budget_usage or {}).items():
        if isinstance(v, int | float) and not isinstance(v, bool):
            budget_usage[k] = budget_usage.get(k, 0) + v


def _existing_scout_fingerprints(
    proposal_repo: ProposalRepository,
    *,
    task_id: str,
) -> set[str]:
    existing_fingerprints: set[str] = set()
    for existing_proposal in proposal_repo.list_proposals(
        task_id=task_id,
        proposal_type=ProposalType.SCOUT,
    ):
        metadata = existing_proposal.metadata_payload
        if not isinstance(metadata, dict):
            continue
        fingerprint = metadata.get("fingerprint")
        if isinstance(fingerprint, str):
            existing_fingerprints.add(fingerprint)
    return existing_fingerprints


def _result_artifacts_payload(
    result: Any,
    fallback_artifacts: list[Any] | None = None,
) -> list[Any]:
    if result is None:
        logger.warning("Worker result is None, falling back to default artifacts")
        source_artifacts = fallback_artifacts or []
    else:
        source_artifacts = result.artifacts or fallback_artifacts or []
    return [artifact.model_dump(mode="json") for artifact in source_artifacts]


def _result_metadata_payload(
    *,
    task: Any,
    result: Any,
    scout_mode: ScoutMode,
    worker_run_id: str,
    fallback_artifacts: list[Any],
    scout_depth: str | None,
    scout_focus: str | None,
    scout_phase: str | None,
    scout_phase_metadata: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    metadata_payload: dict[str, Any] = {
        "source": "scout",
        "scout_mode": scout_mode,
        "task_id": task.id,
        "worker_run_id": worker_run_id,
        "files_changed": result.files_changed or [],
        "artifacts": _result_artifacts_payload(result, fallback_artifacts),
        "budget_usage": result.budget_usage or {},
        "diff_text": result.diff_text,
        "json_payload": result.json_payload,
    }
    if scout_depth:
        metadata_payload["scout_depth"] = scout_depth
    if scout_focus:
        metadata_payload["scout_focus"] = scout_focus
    if scout_phase:
        metadata_payload["scout_phase"] = scout_phase
    if scout_phase_metadata:
        metadata_payload["scout_phase_metadata"] = scout_phase_metadata
    return metadata_payload


def _persist_scout_proposal_item(
    proposal_repo: ProposalRepository,
    *,
    task: Any,
    proposal: ScoutProposal,
    fingerprint: str,
    metadata_payload: dict[str, Any],
) -> Any:
    metadata = dict(metadata_payload)
    metadata["fingerprint"] = fingerprint
    metadata["scout_proposal"] = proposal.model_dump(mode="json")
    return proposal_repo.create_proposal(
        session_id=task.session_id,
        task_id=task.id,
        title=proposal.title,
        summary=proposal.description,
        status=ProposalStatus.PENDING_REVIEW,
        proposal_type=ProposalType.SCOUT,
        metadata_payload=metadata,
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

    assert state.result is not None

    constraints = task.constraints if isinstance(task.constraints, dict) else {}

    raw_mode = constraints.get("scout_mode")
    scout_mode_str = str(raw_mode or "").strip() or "repo"
    if scout_mode_str not in ALLOWED_SCOUT_MODES:
        scout_mode_str = "repo"
    scout_mode = cast(ScoutMode, scout_mode_str)

    raw_depth = constraints.get("scout_depth")
    scout_depth = str(raw_depth or "").strip() or None

    raw_focus = constraints.get("scout_focus")
    scout_focus = str(raw_focus or "").strip() or None

    scout_phase_metadata: list[dict[str, Any]] | None = None

    if scout_mode == "deep":
        scout_phase_metadata = []
        summary_parts: list[str] = []

        files_changed: list[str] = []
        all_artifacts: list[Any] = []
        budget_usage: dict[str, Any] = {}

        for phase_result in state.scout_phase_results or []:
            _merge_scout_phase_result(
                phase_result.result,
                phase_result.phase,
                scout_phase_metadata,
                summary_parts,
                files_changed,
                all_artifacts,
                budget_usage,
            )

        _merge_scout_phase_result(
            state.result,
            state.scout_phase or "research",
            scout_phase_metadata,
            summary_parts,
            files_changed,
            all_artifacts,
            budget_usage,
        )

    max_proposals = scout_max_proposals_from_constraints(constraints)
    existing_fingerprints = _existing_scout_fingerprints(proposal_repo, task_id=task.id)
    result_entries: list[tuple[str | None, Any, list[Any]]] = []
    if scout_mode == "deep":
        for phase_result in state.scout_phase_results or []:
            result_entries.append((phase_result.phase, phase_result.result, []))
        result_entries.append((state.scout_phase or "research", state.result, artifacts))
    else:
        result_entries.append((state.scout_phase, state.result, artifacts))

    for scout_phase, result, fallback_artifacts in result_entries:
        if result is None:
            logger.warning(
                "Scout phase result is None, skipping.",
                extra={"task_id": task.id, "scout_phase": scout_phase},
            )
            continue
        try:
            batch = validate_scout_proposal_payload(
                getattr(result, "json_payload", None),
                max_proposals=max_proposals,
            )
        except ScoutProposalValidationError:
            logger.warning(
                "Failed to validate scout proposal payload, skipping.",
                exc_info=True,
                extra={"task_id": task.id, "scout_phase": scout_phase},
            )
            continue
        metadata_payload = _result_metadata_payload(
            task=task,
            result=result,
            scout_mode=scout_mode,
            worker_run_id=worker_run_id,
            fallback_artifacts=fallback_artifacts,
            scout_depth=scout_depth,
            scout_focus=scout_focus,
            scout_phase=scout_phase,
            scout_phase_metadata=scout_phase_metadata,
        )
        for scout_proposal in batch.proposals:
            if not task.session_id or not task.id:
                logger.warning(
                    "Skipping scout proposal persistence: missing session_id or task_id",
                    extra={"task_id": task.id},
                )
                continue
            fingerprint = compute_scout_proposal_fingerprint(
                scout_proposal,
                task_id=task.id,
                phase=scout_phase,
            )
            if fingerprint in existing_fingerprints:
                continue
            existing_fingerprints.add(fingerprint)
            proposal = _persist_scout_proposal_item(
                proposal_repo,
                task=task,
                proposal=scout_proposal,
                fingerprint=fingerprint,
                metadata_payload=metadata_payload,
            )
            logger.info(
                "Persisted scout proposal",
                extra={
                    "task_id": task.id,
                    "proposal_id": proposal.id,
                    "worker_run_id": worker_run_id,
                    "fingerprint": fingerprint,
                },
            )


def _update_task_route_and_spec(
    task: Any,
    state: OrchestratorState,
    interaction_repo: HumanInteractionRepository,
    plan_repo: ExecutionPlanRepository,
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

    if state.task_plan is not None and state.task_plan.steps:
        # Create plan if it doesn't exist
        if task.execution_plan is None:
            plan = plan_repo.create(task_id=task.id)
            task.execution_plan = plan
        else:
            plan = task.execution_plan

        # Sync nodes
        existing_nodes = {n.node_id: n for n in plan.nodes}
        active_node_ids = set()
        for i, step in enumerate(state.task_plan.steps):
            if not step or not step.step_id:
                logger.warning("Encountered null step or step_id in planner task_plan.")
                continue
            active_node_ids.add(step.step_id)
            if step.step_id not in existing_nodes:
                new_node = plan_repo.add_node(
                    plan_id=plan.id,
                    node_id=step.step_id,
                    goal=step.title or f"Step {step.step_id}",
                    sequence_number=i,
                    acceptance_criteria=step.expected_outcome,
                    depends_on=step.depends_on or [],
                )
                existing_nodes[step.step_id] = new_node
            else:
                existing_node = existing_nodes[step.step_id]
                existing_node.goal = step.title or f"Step {step.step_id}"
                existing_node.sequence_number = i
                existing_node.acceptance_criteria = step.expected_outcome
                existing_node.depends_on = step.depends_on or []

        # Remove orphaned nodes that are no longer in the plan
        for node_id, node in existing_nodes.items():
            if node_id not in active_node_ids:
                plan_repo.session.delete(node)


def _persist_execution_outcome(
    self: Any,
    *,
    task_id: str,
    state: OrchestratorState,
    started_at: datetime,
    finished_at: datetime,
    force_task_status: TaskStatus | None = None,
    persist_friction_proposals: bool = True,
) -> _PersistedExecutionOutcome:
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

        plan_repo = ExecutionPlanRepository(session)

        task = task_repo.get(task_id)
        if task is None:
            raise RuntimeError(f"Task '{task_id}' disappeared while persisting execution.")

        _update_task_route_and_spec(task, state, interaction_repo, plan_repo)

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
        task_constraints_val = (
            dict(task.constraints) if isinstance(task.constraints, dict) else None
        )
        worker_run_id_val = worker_run.id

    persisted_outcome = _PersistedExecutionOutcome(
        task_id=task_id_val,
        session_id=session_id_val,
        task_constraints=task_constraints_val,
        worker_run_id=worker_run_id_val,
    )

    if persist_friction_proposals:
        try:
            _persist_friction_proposals_if_needed(
                self,
                task_id=task_id_val,
                session_id=session_id_val,
                task_constraints=task_constraints_val,
                state=state,
                worker_run_id=worker_run_id_val,
            )
        except Exception as exc:
            logger.warning("Failed to persist friction proposals: %s", exc, exc_info=True)
    self._prune_retained_runs(now=finished_at)
    return persisted_outcome
