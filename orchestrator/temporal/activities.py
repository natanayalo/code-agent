from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from temporalio import activity

from apps.observability import with_restored_trace_context
from db.base import utc_now
from db.enums import (
    ExecutionPlanNodeStatus,
    HumanInteractionHitlMode,
    HumanInteractionStatus,
    HumanInteractionType,
    TaskStatus,
    TimelineEventType,
)
from db.models import ExecutionPlanNodeAttempt, HumanInteraction, Task
from db.utils import compute_interaction_content_hash
from orchestrator.decomposition import is_read_only_fanout_eligible
from orchestrator.execution_graph_input import build_orchestrator_graph_input
from orchestrator.execution_policy import _apply_execution_budget_policy
from orchestrator.execution_resume_service import (
    _reconstruct_single_node_outcome,
    restore_decomposed_plan_from_events,
    restore_merged_node_outcomes,
    restore_task_plan_from_events,
    validate_decomposed_plan_projection,
)
from orchestrator.execution_types import ProgressEvent, ProgressPhase
from orchestrator.graph import (
    _aggregate_decomposed_results,
    _await_worker_with_timeout,
    _build_worker_request,
    _effective_input_evidence,
    _resolve_orchestrator_timeout_seconds,
    _session_state_update_from_result,
    _skipped_node_result,
    build_await_result_node,
    build_decompose_task_node,
    build_generate_task_spec_and_route_node,
    build_load_memory_node,
    build_persist_memory_node,
    build_rejected_session_state_update,
    build_review_result_node,
    check_approval,
    summarize_result,
)
from orchestrator.node_execution import (
    CLAIM_HEARTBEAT_SECONDS,
    NodeActivityClaimLost,
    NodeActivityInProgress,
    NodeActivityRequest,
    NodeActivityResultRef,
    NodeExecutionService,
    logical_activity_key,
)
from orchestrator.nodes.delivery import build_deliver_result_node

# Import nodes and builders
from orchestrator.nodes.ingestion import (
    classify_task,
    ingest_task,
    load_repo_profile_node,
    plan_task,
)
from orchestrator.nodes.provisioning import (
    build_init_environment_node,
    build_provision_workspace_node,
)
from orchestrator.nodes.utils import _available_workers
from orchestrator.nodes.verification import build_verify_result_node
from orchestrator.state import (
    DecomposedTaskPlan,
    NodeOutcome,
    OrchestratorState,
    TaskPlan,
    TaskTimelineEventState,
)
from orchestrator.temporal.completion_loop import (
    CompletionLoopDecision,
    apply_repair_rejection,
    apply_verification_decision,
    decision_from_state,
    verification_is_pending,
)
from orchestrator.temporal.node_wave import (
    DecomposeTaskResult,
    NodeSelectionResult,
    NodeWaveItem,
    NodeWaveMergeRequest,
    NodeWaveMergeResult,
    NodeWaveSelectionV2,
    deterministic_wave_id,
)
from orchestrator.temporal.queues import execution_task_queue_for_profile
from repositories import (
    ExecutionCapacityPermitRepository,
    ExecutionPlanRepository,
    SessionStateRepository,
    TaskRepository,
    TaskTimelineRepository,
    TemporalTaskStateRepository,
    session_scope,
)
from sandbox.scratch import scratch_namespace_component
from workers import ArtifactReference, WorkerResult

logger = logging.getLogger(__name__)

EXECUTION_CAPACITY_LEASE_SECONDS = 60

EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS: frozenset[str] = frozenset(
    {
        "progress_updates",
        "timeline_events",
        "friction_reports",
        "errors",
        "session_state_update",
        "scout_phase_results",
        "memory_to_persist",
        "task_plan",
        "decomposed_plan",
        "node_outcomes",
    }
)


def _serialize_temporal_task_state(state: OrchestratorState) -> dict[str, Any]:
    """Serialize orchestrator state for Temporal activity handoff snapshot storage.

    Excludes transient fields that are not part of intermediate activity handoff contracts.
    """
    return state.model_dump(mode="json", exclude=set(EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS))


def _permission_escalation_retry_is_complete(
    task_id: str,
    task: Any,
    snapshot: Any,
    approved: bool,
) -> bool:
    """Return whether a missing snapshot represents an already-terminal retry."""
    if task is None:
        raise RuntimeError(f"Task '{task_id}' is unavailable for permission escalation.")
    if snapshot is not None:
        return False
    if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise RuntimeError(f"Task '{task_id}' is unavailable for permission escalation.")
    logger.info(
        "Permission escalation already resolved for terminal task",
        extra={
            "task_id": task_id,
            "task_status": task.status.value,
            "approved": approved,
        },
    )
    return True


def _blocked_permission_outcome(state: OrchestratorState) -> NodeOutcome | None:
    return next(
        (
            outcome
            for outcome in state.node_outcomes
            if outcome.status == "blocked"
            and outcome.result.next_action_hint == "request_higher_permission"
        ),
        None,
    )


def _reject_permission_escalation(
    session: Any,
    task_id: str,
    task: Task,
    state: OrchestratorState,
    blocked: NodeOutcome | None,
    plan: Any,
) -> None:
    task.last_error = "Worker permission escalation rejected by operator."
    TaskTimelineRepository(session).create_next_for_attempt(
        task_id=task_id,
        attempt_number=task.attempt_count,
        event_type=TimelineEventType.APPROVAL_REJECTED,
        event_key=f"permission-escalation:{task_id}:rejected",
        message=task.last_error,
    )
    if blocked is not None and plan is not None:
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id=blocked.node_id,
            status=ExecutionPlanNodeStatus.FAILED,
            failure_kind="permission_denied",
        )
    elif plan is not None:
        blocked_sql_node = next(
            (n for n in plan.nodes if n.status == ExecutionPlanNodeStatus.BLOCKED), None
        )
        if blocked_sql_node is not None:
            raise RuntimeError(
                f"Blocked execution plan node {blocked_sql_node.node_id} has no marker-confirmed "
                "blocked outcome in parent state during permission escalation rejection"
            )
    if state.completion_loop.phase == "repair_requested":
        apply_repair_rejection(state)
        task.constraints = state.task.constraints
        task.status = TaskStatus.IN_PROGRESS
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id, state=_serialize_temporal_task_state(state)
        )
        return
    _persist_rejected_session_state(
        session,
        task,
        state,
        initial_approval_rejected=False,
    )
    task.status = TaskStatus.FAILED
    TemporalTaskStateRepository(session).delete(task_id=task_id)


def _persist_rejected_session_state(
    session: Any,
    task: Task,
    state: OrchestratorState,
    *,
    initial_approval_rejected: bool,
) -> None:
    """Persist the typed compact context before a rejection removes resumable state."""
    session_id = state.session.session_id if state.session is not None else task.session_id
    update = (
        build_rejected_session_state_update(state)
        if initial_approval_rejected
        else _session_state_update_from_result(state, state.result)
    )
    SessionStateRepository(session).upsert(
        session_id=session_id,
        active_goal=update.active_goal,
        decisions_made=update.decisions_made,
        identified_risks=update.identified_risks,
        files_touched=update.files_touched,
    )
    logger.info(
        "Persisted compact session state for rejected task",
        extra={"session_id": session_id, "task_id": task.id},
    )


def _approve_permission_escalation(
    session: Any,
    task_id: str,
    task: Task,
    state: OrchestratorState,
    blocked: NodeOutcome | None,
    plan: Any,
) -> None:
    requested = state.result.requested_permission if state.result else None
    constraints = dict(task.constraints or {})
    constraints["granted_permission"] = requested
    constraints["permission_escalation_retry"] = True
    task.constraints = constraints
    task.status = TaskStatus.IN_PROGRESS
    state.task = state.task.model_copy(update={"constraints": constraints})
    if plan is not None:
        if blocked is None:
            blocked_sql_node = next(
                (n for n in plan.nodes if n.status == ExecutionPlanNodeStatus.BLOCKED), None
            )
            if blocked_sql_node is not None:
                raise RuntimeError(
                    f"Blocked execution plan node {blocked_sql_node.node_id} has no "
                    "marker-confirmed blocked outcome in parent state during permission "
                    "escalation approval"
                )
        else:
            ExecutionPlanRepository(session).update_node(
                plan_id=plan.id,
                node_id=blocked.node_id,
                status=ExecutionPlanNodeStatus.PENDING,
                blocker_interaction_id=None,
                retry_count=blocked.attempts,
            )
            # Keep the terminal parent key while the node is reset for a new logical attempt.
            state.result = _aggregate_decomposed_results(state.node_outcomes)
    else:
        state.result = None
    TemporalTaskStateRepository(session).upsert(
        task_id=task_id, state=_serialize_temporal_task_state(state)
    )


def _validate_legacy_outcome_parity(
    canonical: NodeOutcome,
    item: dict[str, Any],
    nid: str,
    key: str,
) -> None:
    snap_outcome = NodeOutcome.model_validate(item)
    if (
        canonical.node_id != snap_outcome.node_id
        or canonical.status != snap_outcome.status
        or canonical.attempts != snap_outcome.attempts
        or (
            snap_outcome.logical_activity_key
            and canonical.logical_activity_key != snap_outcome.logical_activity_key
        )
    ):
        raise RuntimeError(
            f"Legacy snapshot outcome for node {nid} with key '{key}' "
            "conflicts with durable canonical outcome evidence"
        )
    can_res, snap_res = canonical.result, snap_outcome.result
    if (can_res is None) != (snap_res is None):
        raise RuntimeError(
            f"Legacy snapshot outcome for node {nid} with key '{key}' "
            "conflicts with durable canonical outcome evidence"
        )
    if can_res is not None and snap_res is not None:
        if can_res.model_dump(mode="json") != snap_res.model_dump(mode="json"):
            raise RuntimeError(
                f"Legacy snapshot outcome for node {nid} with key '{key}' "
                "conflicts with durable canonical outcome evidence"
            )
    if snap_outcome.result_digest and canonical.result_digest != snap_outcome.result_digest:
        raise RuntimeError(
            f"Legacy snapshot outcome digest for node {nid} with key '{key}' "
            "conflicts with durable canonical outcome evidence"
        )


def _fetch_node_attempts(
    session: Any,
    db_node: Any,
    keys: list[str],
) -> list[Any]:
    existing = [
        a
        for a in getattr(db_node, "attempts", []) or []
        if getattr(a, "logical_activity_key", None) in keys
    ]
    existing_keys = {getattr(a, "logical_activity_key", None) for a in existing}
    missing_keys = [k for k in keys if k not in existing_keys]
    if missing_keys:
        fetched = ExecutionPlanRepository(session).get_attempts_by_activity_keys(
            plan_node_ids=[db_node.id],
            logical_activity_keys=missing_keys,
        )
        existing.extend(fetched)
    return existing


def _should_advance_marker(
    session: Any,
    db_node: Any,
    marker: str,
    key: str,
) -> bool:
    attempts = _fetch_node_attempts(session, db_node, [marker, key])
    marker_attempt = next(
        (a for a in attempts if getattr(a, "logical_activity_key", None) == marker),
        None,
    )
    snap_attempt = next(
        (a for a in attempts if getattr(a, "logical_activity_key", None) == key),
        None,
    )
    if marker_attempt is not None and snap_attempt is not None:
        if snap_attempt.attempt_number > marker_attempt.attempt_number:
            return True
        if snap_attempt.attempt_number < marker_attempt.attempt_number:
            return False
        raise RuntimeError(
            f"Conflicting logical activity keys '{marker}' and '{key}' for node "
            f"{db_node.node_id} at attempt {snap_attempt.attempt_number}"
        )

    if marker == db_node.latest_logical_activity_key:
        return False
    if key == db_node.latest_logical_activity_key:
        return True

    raise RuntimeError(
        f"Cannot determine chronology between marker '{marker}' and snapshot key '{key}' "
        f"for node {db_node.node_id}"
    )


def _bootstrap_single_legacy_outcome(
    session: Any,
    plan: Any,
    item: dict[str, Any],
) -> None:
    nid = item.get("node_id")
    key = item.get("logical_activity_key")
    if not nid:
        return
    if not key:
        raise RuntimeError(
            f"Legacy outcome for node {nid} lacks logical_activity_key and cannot be proven"
        )
    db_node = ExecutionPlanRepository(session).get_node(plan.id, nid)
    if db_node is None:
        raise RuntimeError(f"Legacy outcome node {nid} does not exist in execution plan")

    marker = db_node.merged_logical_activity_key
    if marker == key:
        return
    if marker is not None and not _should_advance_marker(session, db_node, marker, key):
        return

    attempts = _fetch_node_attempts(session, db_node, [key])
    attempt = attempts[0] if attempts else None
    has_term = (
        db_node.latest_logical_activity_key == key and db_node.terminal_result_payload is not None
    )
    if attempt is None and not has_term:
        raise RuntimeError(
            f"Legacy snapshot outcome for node {nid} with key '{key}' "
            "cannot be validated against durable attempt or terminal evidence"
        )

    canonical = _reconstruct_single_node_outcome(db_node, attempt, key)
    _validate_legacy_outcome_parity(canonical, item, nid, key)

    ExecutionPlanRepository(session).update_node(
        plan_id=plan.id,
        node_id=nid,
        merged_logical_activity_key=key,
    )


def _bootstrap_legacy_snapshot_merge_markers(
    session: Any,
    plan: Any,
    raw_snapshot: Any,
) -> None:
    if raw_snapshot is None or not isinstance(getattr(raw_snapshot, "state", None), dict):
        return
    legacy_outcomes = raw_snapshot.state.get("node_outcomes")
    if not (isinstance(legacy_outcomes, list) and legacy_outcomes and plan is not None):
        return
    for item in legacy_outcomes:
        if isinstance(item, dict):
            _bootstrap_single_legacy_outcome(session, plan, item)


def _sync_approval_from_task(
    task: Any,
    state: OrchestratorState,
) -> None:
    approval_data = (task.constraints or {}).get("approval") if task is not None else None
    approval_status = approval_data.get("status") if isinstance(approval_data, dict) else None
    if (
        approval_status in {"approved", "rejected"}
        and state.approval is not None
        and state.approval.required
    ):
        state.approval = state.approval.model_copy(update={"status": approval_status})


def _rehydrate_dag_state(
    session: Any,
    task_id: str,
    state: OrchestratorState,
    raw_snapshot: Any = None,
) -> OrchestratorState:
    """Authoritatively rehydrate timeline, task_plan, decomposed_plan, and node_outcomes.

    Invariants:
    1. Timeline is the exact authority for task_plan and decomposed_plan and replaces any
       older SQL-synthesized models (even on no-snapshot paths).
    2. Wave 3B rehydrates state.node_outcomes from relational merge markers and attempts.
       Wave 3B.2 prunes node_outcomes from snapshot serialization.
    3. Operational execution_plan_nodes in Postgres are validated against restored decomposed_plan.
    """
    task = TaskRepository(session).get(task_id)
    _sync_approval_from_task(task, state)

    timeline_repo = TaskTimelineRepository(session)
    raw_events = timeline_repo.list_by_task(task_id)
    state.timeline_events = [
        TaskTimelineEventState(
            event_type=(
                event.event_type.value
                if hasattr(event.event_type, "value")
                else str(event.event_type)
            ),
            attempt_number=event.attempt_number,
            sequence_number=event.sequence_number,
            message=event.message,
            payload=event.payload,
            created_at=event.created_at,
        )
        for event in raw_events
    ]
    state.timeline_persisted_count = timeline_repo.count_by_attempt(
        task_id=task_id,
        attempt_number=state.attempt_count,
    )

    timeline_task_plan = restore_task_plan_from_events(state.timeline_events)
    if timeline_task_plan is not None:
        state.task_plan = timeline_task_plan
    elif (
        raw_snapshot is not None
        and isinstance(getattr(raw_snapshot, "state", None), dict)
        and raw_snapshot.state.get("task_plan")
    ):
        state.task_plan = TaskPlan.model_validate(raw_snapshot.state["task_plan"])
    else:
        state.task_plan = None

    timeline_decomposed_plan = restore_decomposed_plan_from_events(state.timeline_events)
    if timeline_decomposed_plan is not None:
        state.decomposed_plan = timeline_decomposed_plan
    elif (
        raw_snapshot is not None
        and isinstance(getattr(raw_snapshot, "state", None), dict)
        and raw_snapshot.state.get("decomposed_plan")
    ):
        state.decomposed_plan = DecomposedTaskPlan.model_validate(
            raw_snapshot.state["decomposed_plan"]
        )
    else:
        state.decomposed_plan = None

    if state.decomposed_plan is not None and state.decomposed_plan.status == "decomposed":
        plan = ExecutionPlanRepository(session).get_by_task_id(task_id)
        validate_decomposed_plan_projection(plan, state.decomposed_plan)
        _bootstrap_legacy_snapshot_merge_markers(session, plan, raw_snapshot)
        state.node_outcomes = restore_merged_node_outcomes(plan, session=session)
        if state.result is None and state.node_outcomes:
            state.result = _aggregate_decomposed_results(state.node_outcomes)

    return state


def _resolve_permission_escalation_state(
    session_factory: Any,
    task_id: str,
    approved: bool,
) -> None:
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_id)
        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        if _permission_escalation_retry_is_complete(task_id, task, snapshot, approved):
            return
        assert task is not None and snapshot is not None
        state = OrchestratorState.model_validate(snapshot.state)
        state = _rehydrate_dag_state(session, task_id, state, raw_snapshot=snapshot)
        if (
            not approved
            and state.completion_loop.phase == "manual_follow_up"
            and state.result is not None
            and state.result.next_action_hint == "await_manual_follow_up"
        ):
            return
        blocked = _blocked_permission_outcome(state)
        plan = ExecutionPlanRepository(session).get_by_task_id(task_id)
        if approved:
            _approve_permission_escalation(session, task_id, task, state, blocked, plan)
        else:
            _reject_permission_escalation(session, task_id, task, state, blocked, plan)


async def _send_activity_heartbeats(task_id: str) -> None:
    try:
        while True:
            activity.heartbeat()
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        raise
    except RuntimeError as exc:
        logger.debug("Temporal heartbeat failed for task %s: %s", task_id, exc)
        raise


def _worker_state_for_execution(
    state: OrchestratorState,
    *,
    repair_execution: bool,
) -> dict[str, Any]:
    state_dict = state.model_dump()
    if repair_execution:
        state_dict["verification"] = None
        state_dict["review"] = None
        state_dict["repair_handoff_requested"] = False
    return state_dict


def _finalize_worker_activity_state(
    state_dict: dict[str, Any],
    *,
    repair_execution: bool,
) -> tuple[OrchestratorState, bool]:
    state = OrchestratorState.model_validate(state_dict)
    constraints = state.task.constraints if isinstance(state.task.constraints, dict) else {}
    constraints = dict(constraints)
    constraints.pop("permission_escalation_retry", None)
    state.task = state.task.model_copy(update={"constraints": constraints})
    requires_permission = bool(
        state.result and state.result.next_action_hint == "request_higher_permission"
    )
    if repair_execution and not requires_permission:
        state.completion_loop = state.completion_loop.model_copy(
            update={
                "phase": "verification_pending",
                "summary": "repair worker completed; re-verification is pending",
            }
        )
    return state, requires_permission


def _retain_cancelled_workspace_artifact(state: OrchestratorState) -> OrchestratorState:
    """Use the pinned dispatch workspace when provider cancellation yields no artifact."""
    result = state.result
    if result is None or any(
        artifact.name == "workspace" or artifact.artifact_type == "workspace"
        for artifact in result.artifacts
    ):
        return state
    manifest = state.dispatch.runtime_manifest or {}
    sandbox_manifest = manifest.get("sandbox") or {}
    worker_manifest = manifest.get("worker") or {}
    workspace_root = sandbox_manifest.get("workspace_root")
    workspace_id = state.dispatch.workspace_id or worker_manifest.get("workspace_id")
    if not isinstance(workspace_root, str) or not isinstance(workspace_id, str):
        return state
    workspace_path = Path(workspace_root) / workspace_id
    if not workspace_path.is_absolute():
        return state
    retained_result = result.model_copy(
        update={
            "artifacts": [
                *result.artifacts,
                ArtifactReference(
                    name="workspace",
                    uri=workspace_path.as_uri(),
                    artifact_type="workspace",
                ),
            ]
        }
    )
    return state.model_copy(update={"result": retained_result})


def _source_file_changes(files_changed: list[str], logical_activity_key: str) -> list[str]:
    """Exclude only this node's legacy in-repository scratch paths.

    New node scratch is outside the repository. The exact legacy paths remain
    filtered for resumed workspaces without hiding another node's evidence.
    """
    namespace = scratch_namespace_component(logical_activity_key)
    scratch_prefixes = (
        f".code-agent/node-runs/{namespace}/",
        f".agent_home/{namespace}/",
        f"artifacts/{namespace}/",
    )
    source_paths: list[str] = []
    for path in files_changed:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized.startswith(scratch_prefixes):
            source_paths.append(path)
    return source_paths


def _project_decomposed_runtime_manifest(state: OrchestratorState) -> None:
    """Carry the effective node-wave deployment contract into parent persistence."""
    request = _build_worker_request(state)
    state.dispatch = state.dispatch.model_copy(
        update={"runtime_manifest": request.runtime_manifest}
    )


def _restore_task_trace_context(func: Any) -> Any:
    """Restore the ingress trace context around a Temporal activity invocation."""

    @wraps(func)
    async def _wrapped(self: Any, task_id: str, *args: Any, **kwargs: Any) -> Any:
        trace_context = await self.service._run_blocking(self._load_task_trace_context, task_id)
        with with_restored_trace_context(trace_context):
            return await func(self, task_id, *args, **kwargs)

    return _wrapped


class TaskExecutionActivities:
    def __init__(self, service: Any) -> None:
        self.service = service

        # Build reusable node instances
        available_workers_dict = _available_workers(self.service.worker)
        available_workers = frozenset(available_workers_dict.keys())
        active_profiles = (
            self.service.worker_profiles if self.service.enable_worker_profiles else None
        )
        shell_worker = getattr(
            self.service.worker,
            "get_shell_worker",
            lambda: available_workers_dict.get("shell"),
        )()
        profile_names = (
            frozenset(self.service.worker_profiles.keys())
            if self.service.enable_worker_profiles
            else frozenset()
        )

        self.generate_task_spec_and_route_node = build_generate_task_spec_and_route_node(
            available_workers=available_workers,
            available_profiles=active_profiles,
            orchestrator_brain=self.service.orchestrator_brain,
        )
        self.load_memory_node = (
            build_load_memory_node(self.service.session_factory)
            if self.service.session_factory
            else None
        )
        self.provision_workspace_node = (
            build_provision_workspace_node(workspace_manager=self.service.workspace_manager)
            if self.service.workspace_manager
            else None
        )
        self.init_environment_node = (
            build_init_environment_node(
                workspace_manager=self.service.workspace_manager,
                shell_worker=shell_worker,
            )
            if self.service.workspace_manager
            else None
        )
        self.await_result_node = build_await_result_node(
            self.service.worker,
            available_profile_names=profile_names,
            session_factory=self.service.session_factory,
        )
        self.verify_result_node = build_verify_result_node(
            enable_independent_verifier=self.service.enable_independent_verifier,
            worker=self.service.worker,
            orchestrator_brain=self.service.orchestrator_brain,
        )
        self.review_result_node = build_review_result_node(self.service.worker)
        self.deliver_result_node = build_deliver_result_node(self.service.worker)
        self.persist_memory_node = (
            build_persist_memory_node(self.service.session_factory)
            if self.service.session_factory
            else None
        )
        self.decompose_task_node = (
            build_decompose_task_node(self.service.session_factory)
            if self.service.session_factory
            else None
        )

    def _claim_execution_capacity(self, queue_name: str, owner: str, token: str) -> bool:
        with session_scope(self.service.session_factory) as session:
            return ExecutionCapacityPermitRepository(session).claim(
                queue_name=queue_name,
                owner=owner,
                token=token,
                lease_seconds=EXECUTION_CAPACITY_LEASE_SECONDS,
            )

    def _heartbeat_execution_capacity(self, owner: str, token: str) -> bool:
        with session_scope(self.service.session_factory) as session:
            return ExecutionCapacityPermitRepository(session).heartbeat(
                owner=owner,
                token=token,
                lease_seconds=EXECUTION_CAPACITY_LEASE_SECONDS,
            )

    def _release_execution_capacity(self, owner: str, token: str) -> None:
        with session_scope(self.service.session_factory) as session:
            ExecutionCapacityPermitRepository(session).release(owner=owner, token=token)

    async def _notify_progress(
        self,
        task_id: str,
        *,
        phase: ProgressPhase,
        summary: str | None = None,
    ) -> None:
        """Deliver best-effort product progress from durable Temporal activities."""
        notifier = self.service.progress_notifier
        if notifier is None:
            return
        loaded = await self.service._run_blocking(
            self.service._load_submission_for_task,
            task_id=task_id,
        )
        if loaded is None:
            return
        submission, persisted = loaded
        event = ProgressEvent(
            phase=phase,
            task_id=task_id,
            session_id=persisted.session_id,
            channel=persisted.channel,
            external_thread_id=persisted.external_thread_id,
            task_text=submission.task_text,
            summary=summary,
        )
        try:
            await notifier.notify(submission=submission, event=event)
        except Exception:
            logger.warning(
                "Temporal progress notification failed",
                exc_info=True,
                extra={"task_id": task_id, "phase": phase},
            )

    def _get_current_state(self, task_id: str) -> OrchestratorState:
        with session_scope(self.service.session_factory) as session:
            snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
            if snapshot is not None:
                state = OrchestratorState.model_validate(snapshot.state)
                return _rehydrate_dag_state(session, task_id, state, raw_snapshot=snapshot)

            loaded = self.service._load_submission_for_task(task_id=task_id)
            if not loaded:
                raise RuntimeError(f"Task {task_id} not found")
            submission, persisted = loaded

            timeline_persisted_count = TaskTimelineRepository(session).count_by_attempt(
                task_id=task_id,
                attempt_number=persisted.attempt_count,
            )
            effective_budget = _apply_execution_budget_policy(
                channel=persisted.channel,
                constraints=submission.constraints,
                budget=submission.budget,
            )
            graph_input = build_orchestrator_graph_input(
                submission,
                persisted,
                effective_budget,
                timeline_persisted_count,
            )
            state = OrchestratorState.model_validate(graph_input)
            return _rehydrate_dag_state(session, task_id, state, raw_snapshot=None)

    def _load_task_trace_context(self, task_id: str) -> dict[str, str]:
        with session_scope(self.service.session_factory) as session:
            task = TaskRepository(session).get(task_id)
            return dict(task.trace_context or {}) if task is not None else {}

    def _delete_temporal_snapshot(self, task_id: str) -> None:
        with session_scope(self.service.session_factory) as session:
            TemporalTaskStateRepository(session).delete(task_id=task_id)

    def _persist_state(
        self,
        task_id: str,
        state: OrchestratorState,
        started_at: datetime,
        finished_at: datetime,
        force_status: TaskStatus | None = None,
    ) -> None:
        self.service._persist_execution_outcome(
            task_id=task_id,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
            force_task_status=force_status,
            persist_friction_proposals=False,
        )
        self._delete_temporal_snapshot(task_id)

    def _persist_intermediate_state(
        self,
        task_id: str,
        state: OrchestratorState,
        started_at: datetime,
        finished_at: datetime,
        force_status: TaskStatus | None = None,
        clear_snapshot: bool = False,
    ) -> None:
        from orchestrator.execution_outcome_service import (
            _apply_approval_constraints,
            _persist_timeline_events,
            _update_task_route_and_spec,
        )
        from repositories import (
            ExecutionPlanRepository,
            HumanInteractionRepository,
            TaskRepository,
        )

        with session_scope(self.service.session_factory) as session:
            task_repo = TaskRepository(session)
            interaction_repo = HumanInteractionRepository(session)
            plan_repo = ExecutionPlanRepository(session)

            task = task_repo.get(task_id)
            if task is None:
                raise RuntimeError(f"Task '{task_id}' disappeared.")

            _update_task_route_and_spec(task, state, interaction_repo, plan_repo)
            _apply_approval_constraints(task, state, finished_at)
            if task.status not in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                task.status = force_status or TaskStatus.IN_PROGRESS
            _persist_timeline_events(session, task_id, state)
            if clear_snapshot:
                TemporalTaskStateRepository(session).delete(task_id=task_id)
            else:
                TemporalTaskStateRepository(session).upsert(
                    task_id=task_id, state=_serialize_temporal_task_state(state)
                )

    def _merge_updates(self, state_dict: dict[str, Any], updates: dict[str, Any] | None) -> None:
        if not isinstance(updates, dict):
            return
        for key, val in updates.items():
            if val is None:
                continue
            if key in (
                "timeline_events",
                "progress_updates",
                "friction_reports",
                "memory_to_persist",
                "errors",
                "scout_phase_results",
            ):
                state_dict[key] = list(state_dict.get(key) or []) + list(val)
            else:
                state_dict[key] = val

    async def _run_node(self, node: Any, state_dict: dict[str, Any]) -> dict[str, Any]:
        if node is None:
            return {}
        if hasattr(node, "ainvoke"):
            res = await node.ainvoke(state_dict)
        elif inspect.iscoroutinefunction(node):
            res = await node(state_dict)
        else:

            def _sync_run() -> Any:
                if hasattr(node, "invoke"):
                    return node.invoke(state_dict)
                return node(state_dict)

            res = await self.service._run_blocking(_sync_run)

        if inspect.isawaitable(res):
            res = await res
        return res

    def _has_event(self, state: OrchestratorState, *event_types: TimelineEventType) -> bool:
        vals = {et.value for et in event_types}
        for event in state.timeline_events:
            event_val = (
                event.event_type.value
                if hasattr(event.event_type, "value")
                else str(event.event_type)
            )
            if event_val in vals:
                return True
        return False

    @activity.defn(name="classify_and_plan")
    @_restore_task_trace_context
    async def classify_and_plan(self, task_id: str) -> dict[str, Any]:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        if self._has_event(state, TimelineEventType.TASK_SPEC_AND_ROUTE_GENERATED):
            logger.info("classify_and_plan already executed for task %s, skipping", task_id)
            return {
                "requires_clarification": bool(
                    state.task_spec and state.task_spec.requires_clarification
                ),
                "requires_approval": state.approval.required if state.approval else False,
                "execution_task_queue": execution_task_queue_for_profile(
                    state.route.chosen_profile if state.route else None
                ),
            }

        started_at = utc_now()
        state_dict = state.model_dump()
        for node in [
            ingest_task,
            classify_task,
            plan_task,
            load_repo_profile_node,
            self.generate_task_spec_and_route_node,
            check_approval,
        ]:
            updates = await self._run_node(node, state_dict)
            self._merge_updates(state_dict, updates)

        state = OrchestratorState.model_validate(state_dict)
        finished_at = utc_now()

        await self.service._run_blocking(
            self._persist_intermediate_state,
            task_id=task_id,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
        )
        await self._notify_progress(task_id, phase="started")
        await self._notify_progress(
            task_id,
            phase="awaiting_approval" if state.approval.required else "running",
            summary=state.approval.reason if state.approval.required else None,
        )

        return {
            "requires_clarification": bool(
                state.task_spec and state.task_spec.requires_clarification
            ),
            "requires_approval": state.approval.required if state.approval else False,
            "execution_task_queue": execution_task_queue_for_profile(
                state.route.chosen_profile if state.route else None
            ),
        }

    @activity.defn(name="decompose_task")
    @_restore_task_trace_context
    async def decompose_task(self, task_id: str) -> dict[str, Any]:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        if state.decomposed_plan is not None:
            logger.info("decompose_task already executed for task %s, skipping", task_id)
            return self._decompose_result(state).model_dump(mode="json")

        started_at = utc_now()
        state_dict = state.model_dump()
        updates = await self._run_node(self.decompose_task_node, state_dict)
        self._merge_updates(state_dict, updates)

        state = OrchestratorState.model_validate(state_dict)
        finished_at = utc_now()

        await self.service._run_blocking(
            self._persist_intermediate_state,
            task_id=task_id,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
        )
        return self._decompose_result(state).model_dump(mode="json")

    @staticmethod
    def _decompose_result(state: OrchestratorState) -> DecomposeTaskResult:
        """Keep the workflow branch decision out of direct database reads."""
        decomposed = bool(
            state.decomposed_plan is not None and state.decomposed_plan.status == "decomposed"
        )
        return DecomposeTaskResult(
            execution_shape="decomposed" if decomposed else "monolithic",
            execution_task_queue=execution_task_queue_for_profile(
                state.route.chosen_profile if state.route else None
            ),
        )

    @activity.defn(name="load_memory")
    @_restore_task_trace_context
    async def load_memory(self, task_id: str) -> None:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        if self._has_event(state, TimelineEventType.MEMORY_LOADED):
            logger.info("load_memory already executed for task %s, skipping", task_id)
            return

        started_at = utc_now()
        state_dict = state.model_dump()
        updates = await self._run_node(self.load_memory_node, state_dict)
        self._merge_updates(state_dict, updates)

        state = OrchestratorState.model_validate(state_dict)
        finished_at = utc_now()

        await self.service._run_blocking(
            self._persist_intermediate_state,
            task_id=task_id,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
        )

    @activity.defn(name="provision_workspace")
    @_restore_task_trace_context
    async def provision_workspace(self, task_id: str) -> None:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        task_constraints = (
            state.task.constraints if isinstance(state.task.constraints, dict) else {}
        )
        retrying_permission_escalation = bool(task_constraints.get("permission_escalation_retry"))
        repair_requested = state.completion_loop.phase == "repair_requested"
        if (
            self._has_event(state, TimelineEventType.WORKSPACE_PROVISIONED)
            and not retrying_permission_escalation
            and not repair_requested
        ):
            logger.info("provision_workspace already executed for task %s, skipping", task_id)
            return

        started_at = utc_now()
        state_dict = state.model_dump()
        for node in [self.provision_workspace_node, self.init_environment_node]:
            updates = await self._run_node(node, state_dict)
            self._merge_updates(state_dict, updates)

        state = OrchestratorState.model_validate(state_dict)
        finished_at = utc_now()

        await self.service._run_blocking(
            self._persist_intermediate_state,
            task_id=task_id,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
        )

    @activity.defn(name="run_worker")
    @_restore_task_trace_context
    async def run_worker(self, task_id: str) -> dict[str, bool]:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        task_constraints = (
            state.task.constraints if isinstance(state.task.constraints, dict) else {}
        )
        retrying_permission_escalation = bool(task_constraints.get("permission_escalation_retry"))
        repair_execution = state.completion_loop.phase == "repair_requested"
        requires_permission = bool(
            state.result and state.result.next_action_hint == "request_higher_permission"
        )
        if (
            self._has_event(
                state,
                TimelineEventType.WORKER_COMPLETED,
                TimelineEventType.WORKER_FAILED,
                TimelineEventType.WORKER_ERROR,
            )
            and not retrying_permission_escalation
            and (not repair_execution or requires_permission)
        ):
            logger.info("run_worker already executed for task %s, skipping", task_id)
            return {"requires_permission_escalation": requires_permission}

        heartbeat_task = asyncio.create_task(
            _send_activity_heartbeats(task_id), name=f"temporal-worker-heartbeat-{task_id}"
        )
        worker_task: asyncio.Task[dict[str, Any]] | None = None
        try:
            started_at = utc_now()
            state_dict = _worker_state_for_execution(
                state,
                repair_execution=repair_execution,
            )
            worker_task = asyncio.create_task(
                self._run_node(self.await_result_node, state_dict),
                name=f"temporal-worker-execution-{task_id}",
            )
            done, _ = await asyncio.wait(
                {worker_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                heartbeat_error = heartbeat_task.exception()
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
                raise heartbeat_error or RuntimeError("Temporal heartbeat stopped unexpectedly.")
            updates = await worker_task
            self._merge_updates(state_dict, updates)

            state, requires_permission = _finalize_worker_activity_state(
                state_dict,
                repair_execution=repair_execution,
            )
            finished_at = utc_now()

            await self.service._run_blocking(
                self._persist_intermediate_state,
                task_id=task_id,
                state=state,
                started_at=started_at,
                finished_at=finished_at,
            )
            return {"requires_permission_escalation": requires_permission}
        except asyncio.CancelledError:
            await self._persist_cancelled_worker_activity(
                task_id=task_id,
                state=state,
                worker_task=worker_task,
                repair_execution=repair_execution,
                started_at=started_at,
            )
            raise
        finally:
            if worker_task is not None and not worker_task.done():
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _persist_cancelled_worker_activity(
        self,
        *,
        task_id: str,
        state: OrchestratorState,
        worker_task: asyncio.Task[dict[str, Any]] | None,
        repair_execution: bool,
        started_at: datetime,
    ) -> None:
        """Retain a cancelled worker's partial result before acknowledging cancellation."""
        if worker_task is None:
            return
        if not worker_task.done():
            worker_task.cancel()
        settled = await asyncio.gather(worker_task, return_exceptions=True)
        updates = settled[0]
        if not isinstance(updates, dict):
            logger.warning(
                "Cancelled worker did not yield partial evidence",
                extra={"task_id": task_id},
            )
            return

        state_dict = _worker_state_for_execution(
            state,
            repair_execution=repair_execution,
        )
        self._merge_updates(state_dict, updates)
        cancelled_state, _ = _finalize_worker_activity_state(
            state_dict,
            repair_execution=repair_execution,
        )
        cancelled_state = _retain_cancelled_workspace_artifact(cancelled_state)
        # The operator cancellation is already the authoritative terminal timeline
        # event. The worker update was produced from the pre-cancellation snapshot,
        # so do not let its stale sequence collide with that durable event.
        cancelled_state.timeline_persisted_count = len(
            [
                e
                for e in cancelled_state.timeline_events
                if e.attempt_number == cancelled_state.attempt_count
            ]
        )
        await self.service._run_blocking(
            self._persist_state,
            task_id=task_id,
            state=cancelled_state,
            started_at=started_at,
            finished_at=utc_now(),
            force_status=TaskStatus.CANCELLED,
        )

    @activity.defn(name="select_next_node")
    @_restore_task_trace_context
    async def select_next_node(self, task_id: str) -> dict[str, Any]:
        """Select one legacy M25.1B node action with its original input shape."""
        return await self._select_next_node(task_id, fanout_contract_enabled=False)

    @activity.defn(name="select_next_node_v2")
    @_restore_task_trace_context
    async def select_next_node_v2(self, task_id: str) -> dict[str, Any]:
        """Select a versioned V2 wave after the workflow patch marker is recorded."""
        return await self._select_next_node(task_id, fanout_contract_enabled=True)

    async def _select_next_node(
        self, task_id: str, *, fanout_contract_enabled: bool
    ) -> dict[str, Any]:
        """Read durable state and choose one deterministic node-wave action."""
        state = await self.service._run_blocking(self._get_current_state, task_id)
        if state.decomposed_plan is None or state.decomposed_plan.status != "decomposed":
            return NodeSelectionResult(
                action="invalid", reason="Task is not decomposed."
            ).model_dump(mode="json")

        def _select() -> NodeSelectionResult | NodeWaveSelectionV2:
            with session_scope(self.service.session_factory) as session:
                plan = ExecutionPlanRepository(session).get_by_task_id(task_id)
                if plan is None:
                    return NodeSelectionResult(
                        action="invalid", reason="Execution plan is missing."
                    )
                plan_nodes = {node.node_id: node for node in plan.nodes}
                state_nodes = {node.node_id: node for node in state.decomposed_plan.nodes}
                if set(plan_nodes) != set(state_nodes):
                    return NodeSelectionResult(
                        action="invalid", reason="Plan nodes do not match state."
                    )
                outcomes = {outcome.node_id: outcome for outcome in state.node_outcomes}
                has_pending_node = False
                for node in plan.nodes:
                    if (
                        node.latest_logical_activity_key
                        and node.terminal_result_payload
                        and node.latest_logical_activity_key != node.merged_logical_activity_key
                    ):
                        return NodeSelectionResult(
                            action="merge_terminal",
                            node_id=node.node_id,
                            logical_activity_key=node.latest_logical_activity_key,
                            result_digest=node.terminal_result_digest,
                        )
                    if node.status == ExecutionPlanNodeStatus.BLOCKED:
                        return NodeSelectionResult(
                            action="await_permission",
                            node_id=node.node_id,
                            logical_activity_key=node.latest_logical_activity_key,
                        )
                    if node.status != ExecutionPlanNodeStatus.PENDING:
                        continue
                    has_pending_node = True
                    dependencies = list(node.depends_on or [])
                    unresolved = [
                        dependency
                        for dependency in dependencies
                        if plan_nodes[dependency].status
                        in {
                            ExecutionPlanNodeStatus.PENDING,
                            ExecutionPlanNodeStatus.ACTIVE,
                            ExecutionPlanNodeStatus.BLOCKED,
                        }
                    ]
                    if unresolved:
                        continue
                    failed = [
                        dependency
                        for dependency in dependencies
                        if plan_nodes[dependency].status
                        in {ExecutionPlanNodeStatus.FAILED, ExecutionPlanNodeStatus.SKIPPED}
                    ]
                    if failed:
                        return NodeSelectionResult(
                            action="skip", node_id=node.node_id, failed_dependency_ids=failed
                        )
                    missing_outcomes = [
                        dependency for dependency in dependencies if dependency not in outcomes
                    ]
                    if missing_outcomes:
                        return NodeSelectionResult(
                            action="invalid",
                            reason="Completed dependency outcomes are missing from parent state.",
                        )
                    node_contract = state_nodes[node.node_id]
                    prior_context = {
                        dependency: {
                            "summary": outcomes[dependency].result.summary,
                            "files_changed": outcomes[dependency].result.files_changed or [],
                            "artifacts": [
                                artifact.model_dump(mode="json")
                                for artifact in (outcomes[dependency].result.artifacts or [])
                            ],
                        }
                        for dependency in dependencies
                    }
                    _evidence, digest = _effective_input_evidence(
                        state, node_contract, prior_context
                    )
                    logical_attempt = node.retry_count + 1
                    task = session.get(Task, task_id)
                    trace_context = task.trace_context if task is not None else None
                    traceparent = (
                        trace_context.get("traceparent")
                        if isinstance(trace_context, dict)
                        else None
                    )
                    trace_parts = traceparent.split("-") if isinstance(traceparent, str) else []
                    request = NodeActivityRequest(
                        task_id=task_id,
                        plan_id=plan.id,
                        node_id=node.node_id,
                        logical_attempt=logical_attempt,
                        logical_activity_key=logical_activity_key(
                            plan.id, node.node_id, logical_attempt
                        ),
                        effective_input_digest=digest,
                        task_trace_id=trace_parts[1] if len(trace_parts) > 1 else None,
                    )
                    singleton = NodeSelectionResult(
                        action="execute",
                        activity_request=request,
                        execution_task_queue=execution_task_queue_for_profile(
                            state.route.chosen_profile if state.route else None
                        ),
                        node_id=node.node_id,
                        logical_activity_key=request.logical_activity_key,
                    )
                    node_state = state.model_copy(
                        update={"task_plan": None, "task_spec": node_contract.task_spec}
                    )
                    node_task_text = (
                        f"Parent task:\n{state.normalized_task_text or state.task.task_text}\n\n"
                        f"Current DAG node ({node.node_id}): {node_contract.task_spec.goal}\n"
                        "Node acceptance criteria: "
                        f"{'; '.join(node_contract.task_spec.acceptance_criteria)}"
                    )
                    effective_request = _build_worker_request(
                        node_state,
                        task_spec_override=node_contract.task_spec,
                        task_text_override=node_task_text,
                        prior_node_context=prior_context,
                    )
                    effective_profile = self.service.worker_profiles.get(
                        effective_request.worker_profile
                    )
                    effective_queue = execution_task_queue_for_profile(
                        effective_request.worker_profile
                    )
                    singleton = singleton.model_copy(
                        update={"execution_task_queue": effective_queue}
                    )
                    effective_manifest = effective_request.runtime_manifest or {}
                    effective_read_only = bool(effective_request.read_only) and (
                        effective_manifest.get("task", {}).get("read_only") is True
                    )
                    if not (
                        fanout_contract_enabled
                        and self.service.decomposed_fanout_enabled
                        and effective_profile is not None
                        and is_read_only_fanout_eligible(
                            parent_read_only=effective_read_only,
                            selected_profile_mutation_policy=effective_profile.mutation_policy,
                            node=node_contract,
                            completed_node_ids={
                                node_id
                                for node_id, persisted in plan_nodes.items()
                                if persisted.status == ExecutionPlanNodeStatus.COMPLETED
                            },
                            has_unresolved_blocker=any(
                                persisted.status == ExecutionPlanNodeStatus.BLOCKED
                                for persisted in plan.nodes
                            ),
                            fanout_disabled=state.fanout_disabled_for_remainder,
                        )
                    ):
                        return singleton
                    # Pilot rule: only inspect the immediately following ready
                    # node. This deliberately never overtakes an ineligible node.
                    position = plan.nodes.index(node)
                    if position + 1 >= len(plan.nodes):
                        return singleton
                    second = plan.nodes[position + 1]
                    second_contract = state_nodes[second.node_id]
                    second_dependencies = list(second.depends_on or [])
                    if second.status != ExecutionPlanNodeStatus.PENDING or any(
                        plan_nodes[dependency].status != ExecutionPlanNodeStatus.COMPLETED
                        for dependency in second_dependencies
                    ):
                        return singleton
                    second_context = {
                        dependency: {
                            "summary": outcomes[dependency].result.summary,
                            "files_changed": outcomes[dependency].result.files_changed or [],
                            "artifacts": [
                                artifact.model_dump(mode="json")
                                for artifact in (outcomes[dependency].result.artifacts or [])
                            ],
                        }
                        for dependency in second_dependencies
                    }
                    _evidence, second_digest = _effective_input_evidence(
                        state, second_contract, second_context
                    )
                    second_node_state = state.model_copy(
                        update={"task_plan": None, "task_spec": second_contract.task_spec}
                    )
                    second_task_text = (
                        f"Parent task:\n{state.normalized_task_text or state.task.task_text}\n\n"
                        f"Current DAG node ({second.node_id}): {second_contract.task_spec.goal}\n"
                        "Node acceptance criteria: "
                        f"{'; '.join(second_contract.task_spec.acceptance_criteria)}"
                    )
                    second_effective_request = _build_worker_request(
                        second_node_state,
                        task_spec_override=second_contract.task_spec,
                        task_text_override=second_task_text,
                        prior_node_context=second_context,
                    )
                    second_profile = self.service.worker_profiles.get(
                        second_effective_request.worker_profile
                    )
                    second_manifest = second_effective_request.runtime_manifest or {}
                    second_read_only = bool(second_effective_request.read_only) and (
                        second_manifest.get("task", {}).get("read_only") is True
                    )
                    queue = effective_queue
                    second_queue = execution_task_queue_for_profile(
                        second_effective_request.worker_profile
                    )
                    if (
                        second_profile is None
                        or not second_read_only
                        or not is_read_only_fanout_eligible(
                            parent_read_only=second_read_only,
                            selected_profile_mutation_policy=second_profile.mutation_policy,
                            node=second_contract,
                            completed_node_ids={
                                node_id
                                for node_id, persisted in plan_nodes.items()
                                if persisted.status == ExecutionPlanNodeStatus.COMPLETED
                            },
                            has_unresolved_blocker=False,
                            fanout_disabled=state.fanout_disabled_for_remainder,
                        )
                    ):
                        return singleton
                    second_request = NodeActivityRequest(
                        task_id=task_id,
                        plan_id=plan.id,
                        node_id=second.node_id,
                        logical_attempt=second.retry_count + 1,
                        logical_activity_key=logical_activity_key(
                            plan.id, second.node_id, second.retry_count + 1
                        ),
                        effective_input_digest=second_digest,
                        task_trace_id=trace_parts[1] if len(trace_parts) > 1 else None,
                        execution_capacity_key=second_queue,
                    )
                    items = [
                        NodeWaveItem(
                            node_id=node.node_id,
                            activity_request=request.model_copy(
                                update={"execution_capacity_key": queue}
                            ),
                            execution_task_queue=queue,
                        ),
                        NodeWaveItem(
                            node_id=second.node_id,
                            activity_request=second_request,
                            execution_task_queue=second_queue,
                        ),
                    ]
                    return NodeWaveSelectionV2(
                        action="execute_wave",
                        items=items,
                        wave_id=deterministic_wave_id(plan.id, items),
                        fanout_applied=True,
                    )
                if has_pending_node:
                    return NodeSelectionResult(
                        action="invalid",
                        reason=(
                            "Execution plan contains pending nodes with unresolvable dependencies."
                        ),
                    )
                return NodeSelectionResult(action="complete")

        selection = await self.service._run_blocking(_select)
        return selection.model_dump(mode="json")

    @activity.defn(name="run_decomposed_node")
    @_restore_task_trace_context
    async def run_decomposed_node(
        self, task_id: str, activity_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute one durable logical node without writing parent snapshot state."""
        node_activity = NodeActivityRequest.model_validate(activity_data)
        state = await self.service._run_blocking(self._get_current_state, task_id)
        if node_activity.task_id != task_id or state.decomposed_plan is None:
            raise ValueError("Node activity does not belong to a decomposed task.")
        node = next(
            (item for item in state.decomposed_plan.nodes if item.node_id == node_activity.node_id),
            None,
        )
        if node is None:
            raise ValueError("Node activity references an unknown plan node.")
        outcome_by_id = {outcome.node_id: outcome for outcome in state.node_outcomes}
        dependencies: list[NodeOutcome] = []
        for dependency in node.depends_on or []:
            outcome = outcome_by_id.get(dependency)
            if outcome is None:
                raise ValueError(f"Dependency {dependency} outcome is missing from state.")
            dependencies.append(outcome)
        prior_context = {
            dependency.node_id: {
                "summary": dependency.result.summary,
                "files_changed": dependency.result.files_changed or [],
                "artifacts": [
                    artifact.model_dump(mode="json")
                    for artifact in (dependency.result.artifacts or [])
                ],
            }
            for dependency in dependencies
        }
        node_state = state.model_copy(update={"task_plan": None, "task_spec": node.task_spec})
        task_text = (
            f"Parent task:\n{state.normalized_task_text or state.task.task_text}\n\n"
            f"Current DAG node ({node.node_id}): {node.task_spec.goal}\n"
            f"Node acceptance criteria: {'; '.join(node.task_spec.acceptance_criteria)}"
        )
        request = _build_worker_request(
            node_state,
            task_spec_override=node.task_spec,
            task_text_override=task_text,
            prior_node_context=prior_context,
        )
        request = request.model_copy(
            update={"scratch_namespace": node_activity.logical_activity_key}
        )
        evidence, digest = _effective_input_evidence(state, node, prior_context)
        if digest != node_activity.effective_input_digest:
            raise ValueError("Node activity input digest changed before execution.")

        async def _execute_worker() -> WorkerResult:
            result, _progress = await _await_worker_with_timeout(
                self.service.worker,
                request,
                worker_type=state.dispatch.worker_type or state.route.chosen_worker or "unknown",
                session_id=request.session_id,
                timeout_seconds=_resolve_orchestrator_timeout_seconds(state),
            )
            source_files_changed = _source_file_changes(
                result.files_changed, node_activity.logical_activity_key
            )
            if node.parallel_safe and (
                source_files_changed
                or result.diff_text
                or (result.delivery_metadata if hasattr(result, "delivery_metadata") else None)
            ):
                return result.model_copy(
                    update={
                        "status": "failure",
                        "failure_kind": "read_only_violation",
                        "summary": "Read-only fan-out node reported mutation evidence.",
                    }
                )
            return result.model_copy(update={"files_changed": source_files_changed})

        active_permit_token: str | None = None

        async def _execute_under_claim_recovery() -> (
            tuple[NodeActivityResultRef, NodeOutcome | None]
        ):
            nonlocal active_permit_token
            while True:
                permit_token: str | None = None
                capacity_claimed = False
                if node_activity.execution_capacity_key:
                    permit_token = uuid4().hex
                    claimed = await self.service._run_blocking(
                        self._claim_execution_capacity,
                        node_activity.execution_capacity_key,
                        node_activity.logical_activity_key,
                        permit_token,
                    )
                    if not claimed:
                        await asyncio.sleep(CLAIM_HEARTBEAT_SECONDS)
                        continue
                    capacity_claimed = True
                    active_permit_token = permit_token
                try:
                    return await NodeExecutionService(self.service.session_factory).execute(
                        activity=node_activity,
                        request=request,
                        effective_input_summary=evidence,
                        execute_worker=_execute_worker,
                    )
                except (NodeActivityInProgress, NodeActivityClaimLost):
                    # A prior activity attempt can retain the fenced DB claim for
                    # up to its lease. Keep the Temporal activity alive until it
                    # either records a terminal payload or the claim can be taken
                    # over with the same logical key.
                    await asyncio.sleep(CLAIM_HEARTBEAT_SECONDS)
                finally:
                    if node_activity.execution_capacity_key and capacity_claimed and permit_token:
                        active_permit_token = None
                        await self.service._run_blocking(
                            self._release_execution_capacity,
                            node_activity.logical_activity_key,
                            permit_token,
                        )

        async def _heartbeat() -> None:
            while True:
                activity.heartbeat()
                if node_activity.execution_capacity_key and active_permit_token:
                    permit_token = active_permit_token
                    renewed = await self.service._run_blocking(
                        self._heartbeat_execution_capacity,
                        node_activity.logical_activity_key,
                        permit_token,
                    )
                    if not renewed and active_permit_token == permit_token:
                        raise NodeActivityClaimLost("Execution capacity permit heartbeat was lost.")
                await asyncio.sleep(5)

        heartbeat = asyncio.create_task(_heartbeat())
        worker_task: asyncio.Task[tuple[NodeActivityResultRef, NodeOutcome | None]] | None = None
        try:
            worker_task = asyncio.create_task(_execute_under_claim_recovery())
            done, _ = await asyncio.wait(
                {worker_task, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                heartbeat_error = heartbeat.exception()
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
                raise heartbeat_error or RuntimeError("Temporal heartbeat stopped unexpectedly.")
            result_ref, _outcome = await worker_task
            return result_ref.model_dump(mode="json")
        finally:
            if worker_task is not None and not worker_task.done():
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    @activity.defn(name="merge_node_wave")
    @_restore_task_trace_context
    async def merge_node_wave(self, task_id: str, merge_data: dict[str, Any]) -> dict[str, Any]:
        """Validate durable node evidence and atomically project the parent state."""
        raw_selection = merge_data.get("selection")
        if isinstance(raw_selection, dict) and raw_selection.get("schema_version") == 2:
            return await self.service._run_blocking(
                self._merge_v2_wave, task_id, raw_selection, merge_data.get("result_refs") or []
            )
        merge = NodeWaveMergeRequest.model_validate(merge_data)
        selection = merge.selection
        state = await self.service._run_blocking(self._get_current_state, task_id)
        if state.decomposed_plan is None or not selection.node_id:
            raise ValueError("Node-wave merge requires a decomposed node selection.")
        node_id = selection.node_id

        def _merge() -> NodeWaveMergeResult:
            with session_scope(self.service.session_factory) as session:
                plan = ExecutionPlanRepository(session).get_by_task_id(task_id)
                if plan is None:
                    raise ValueError("Execution plan is missing.")
                node = ExecutionPlanRepository(session).get_node(plan.id, node_id)
                if node is None:
                    raise ValueError("Execution plan node is missing.")
                contract = next(
                    (
                        item
                        for item in state.decomposed_plan.nodes or []
                        if item.node_id == node.node_id
                    ),
                    None,
                )
                if contract is None:
                    raise ValueError(f"Node contract for {node.node_id} is missing from state.")
                key: str | None
                if selection.action == "skip":
                    result = _skipped_node_result(
                        contract, ", ".join(selection.failed_dependency_ids)
                    )
                    skip_payload = {
                        "schema_version": 1,
                        "worker_result": result.model_dump(mode="json"),
                        "node_outcome": NodeOutcome(
                            node_id=node.node_id,
                            status="skipped",
                            result=result,
                            dependencies=list(node.depends_on or []),
                        ).model_dump(mode="json"),
                        "continuation": "continue",
                    }
                    digest = hashlib.sha256(
                        json.dumps(skip_payload, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    key = f"node-skip:v1:{plan.id}:{node.node_id}:{digest}"
                    ExecutionPlanRepository(session).update_node(
                        plan_id=plan.id,
                        node_id=node.node_id,
                        status=ExecutionPlanNodeStatus.SKIPPED,
                        failure_kind="dependency_failed",
                        result_summary=result.summary,
                        latest_logical_activity_key=key,
                        merged_logical_activity_key=key,
                        terminal_result_schema_version=1,
                        terminal_result_digest=digest,
                        terminal_result_payload=skip_payload,
                    )
                else:
                    key = (
                        merge.result_ref.logical_activity_key
                        if merge.result_ref is not None
                        else selection.logical_activity_key
                    )
                    if not key:
                        raise ValueError("Terminal merge is missing its activity key.")
                    terminal_payload = node.terminal_result_payload
                    terminal_digest = node.terminal_result_digest
                    if not terminal_payload or not terminal_digest:
                        raise ValueError("Terminal node result is unavailable for merge.")
                    attempt = session.scalar(
                        select(ExecutionPlanNodeAttempt).where(
                            ExecutionPlanNodeAttempt.plan_node_id == node.id,
                            ExecutionPlanNodeAttempt.logical_activity_key == key,
                        )
                    )
                    expected = (
                        merge.result_ref.result_digest
                        if merge.result_ref
                        else selection.result_digest
                    )
                    if expected and expected != terminal_digest:
                        raise ValueError("Node result digest does not match durable evidence.")
                    WorkerResult.model_validate(terminal_payload["worker_result"])
                    NodeOutcome.model_validate(terminal_payload["node_outcome"])
                    if attempt is not None and attempt.result_digest != terminal_digest:
                        raise ValueError(
                            "Node attempt digest does not match terminal node evidence."
                        )
                    digest = terminal_digest
                    ExecutionPlanRepository(session).update_node(
                        plan_id=plan.id,
                        node_id=node.node_id,
                        merged_logical_activity_key=key,
                    )

                outcomes = restore_merged_node_outcomes(plan, session=session)
                state.node_outcomes = outcomes
                state.result = _aggregate_decomposed_results(outcomes)
                current = next(
                    (outcome for outcome in outcomes if outcome.node_id == node.node_id), None
                )
                if current is None:
                    raise ValueError(f"Outcome for node {node.node_id} was not found after merge.")
                if current.status == "blocked":
                    if current.attempts >= contract.max_attempts:
                        ExecutionPlanRepository(session).update_node(
                            plan_id=plan.id,
                            node_id=node.node_id,
                            status=ExecutionPlanNodeStatus.FAILED,
                            failure_kind="permission_escalation_exhausted",
                            finished_at=utc_now(),
                        )
                        return NodeWaveMergeResult(continuation="fail_task")
                    return NodeWaveMergeResult(
                        continuation="await_permission",
                        blocked_node_id=node.node_id,
                        blocked_logical_activity_key=key,
                        requested_permission=current.result.requested_permission,
                    )
                if current.result.failure_kind == "read_only_violation":
                    state.fanout_disabled_for_remainder = True
                    return NodeWaveMergeResult(continuation="fail_task")
                if current.status == "failed" and current.attempts < contract.max_attempts:
                    ExecutionPlanRepository(session).update_node(
                        plan_id=plan.id,
                        node_id=node.node_id,
                        status=ExecutionPlanNodeStatus.PENDING,
                        retry_count=current.attempts,
                    )
                    return NodeWaveMergeResult(continuation="retry_node")
                return NodeWaveMergeResult(continuation="continue")

        result = await self.service._run_blocking(_merge)
        _project_decomposed_runtime_manifest(state)
        await self.service._run_blocking(
            self._persist_intermediate_state,
            task_id=task_id,
            state=state,
            started_at=utc_now(),
            finished_at=utc_now(),
        )
        return result.model_dump(mode="json")

    def _merge_v2_wave(
        self,
        task_id: str,
        selection_data: dict[str, Any],
        result_refs: list[dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Project every fan-out result and the parent snapshot in one transaction."""
        selection = NodeWaveSelectionV2.model_validate(selection_data)
        refs = [
            NodeActivityResultRef.model_validate(item) if item is not None else None
            for item in result_refs
        ]
        if len(selection.items) != len(refs):
            raise ValueError("Fan-out merge result count does not match its selection.")
        with session_scope(self.service.session_factory) as session:
            snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
            if snapshot is None:
                raise RuntimeError(f"Task '{task_id}' has no Temporal state.")
            state = OrchestratorState.model_validate(snapshot.state)
            state = _rehydrate_dag_state(session, task_id, state, raw_snapshot=snapshot)
            plan = ExecutionPlanRepository(session).get_by_task_id(task_id)
            if plan is None:
                raise ValueError("Execution plan is missing.")
            if state.decomposed_plan is None:
                raise ValueError("Task is not decomposed.")
            contracts = {node.node_id: node for node in state.decomposed_plan.nodes or []}
            missing_evidence: set[str] = set()
            for item, result_ref in zip(selection.items, refs, strict=True):
                node = ExecutionPlanRepository(session).get_node(plan.id, item.node_id)
                if (
                    node is None
                    or not node.terminal_result_payload
                    or not node.terminal_result_digest
                    or node.latest_logical_activity_key
                    != item.activity_request.logical_activity_key
                ):
                    missing_evidence.add(item.node_id)
                    continue
                if result_ref is not None and (
                    result_ref.node_id != item.node_id
                    or result_ref.logical_activity_key != item.activity_request.logical_activity_key
                ):
                    raise ValueError("Fan-out result does not belong to its selected node.")
                if (
                    result_ref is not None
                    and result_ref.result_digest != node.terminal_result_digest
                ):
                    raise ValueError("Fan-out result digest does not match durable evidence.")
                WorkerResult.model_validate(node.terminal_result_payload["worker_result"])
                NodeOutcome.model_validate(node.terminal_result_payload["node_outcome"])
            for item in selection.items:
                if item.node_id not in missing_evidence:
                    continue
                worker_res = WorkerResult(
                    status="failure",
                    failure_kind="sandbox_infra",
                    summary="Fan-out activity ended without durable terminal evidence.",
                )
                missing_node = next(
                    (node for node in plan.nodes if node.node_id == item.node_id), None
                )
                missing_outcome = NodeOutcome(
                    node_id=item.node_id,
                    status="failed",
                    result=worker_res,
                    dependencies=list(missing_node.depends_on or []) if missing_node else [],
                    attempts=item.activity_request.logical_attempt,
                    logical_activity_key=item.activity_request.logical_activity_key,
                )
                missing_payload = {
                    "schema_version": 1,
                    "worker_result": worker_res.model_dump(mode="json"),
                    "node_outcome": missing_outcome.model_dump(mode="json"),
                    "continuation": "fail_task",
                }
                missing_digest = hashlib.sha256(
                    json.dumps(missing_payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                ExecutionPlanRepository(session).update_node(
                    plan_id=plan.id,
                    node_id=item.node_id,
                    status=ExecutionPlanNodeStatus.FAILED,
                    failure_kind="sandbox_infra",
                    result_summary=worker_res.summary,
                    finished_at=utc_now(),
                    latest_logical_activity_key=item.activity_request.logical_activity_key,
                    terminal_result_schema_version=1,
                    terminal_result_digest=missing_digest,
                    terminal_result_payload=missing_payload,
                    merged_logical_activity_key=item.activity_request.logical_activity_key,
                )
            for item in selection.items:
                if item.node_id in missing_evidence:
                    continue
                ExecutionPlanRepository(session).update_node(
                    plan_id=plan.id,
                    node_id=item.node_id,
                    merged_logical_activity_key=item.activity_request.logical_activity_key,
                )
            outcomes = restore_merged_node_outcomes(plan, session=session)
            state.node_outcomes = outcomes
            state.result = _aggregate_decomposed_results(outcomes)
            continuation: Literal["continue", "retry_node", "await_permission", "fail_task"] = (
                "continue"
            )
            blocked: NodeOutcome | None = None
            if missing_evidence:
                continuation = "fail_task"
            for outcome in outcomes:
                contract = contracts.get(outcome.node_id)
                if outcome.result.failure_kind == "read_only_violation":
                    state.fanout_disabled_for_remainder = True
                    continuation = "fail_task"
                if outcome.status == "blocked" and blocked is None:
                    blocked = outcome
                    state.fanout_disabled_for_remainder = True
                    if contract is None or outcome.attempts >= contract.max_attempts:
                        ExecutionPlanRepository(session).update_node(
                            plan_id=plan.id,
                            node_id=outcome.node_id,
                            status=ExecutionPlanNodeStatus.FAILED,
                            failure_kind="permission_escalation_exhausted",
                            finished_at=utc_now(),
                        )
                        continuation = "fail_task"
                    elif continuation != "fail_task":
                        continuation = "await_permission"
                elif (
                    outcome.status == "failed"
                    and contract
                    and outcome.attempts < contract.max_attempts
                    and outcome.result.failure_kind != "read_only_violation"
                    and continuation != "fail_task"
                ):
                    ExecutionPlanRepository(session).update_node(
                        plan_id=plan.id,
                        node_id=outcome.node_id,
                        status=ExecutionPlanNodeStatus.PENDING,
                        retry_count=outcome.attempts,
                    )
                    if continuation == "continue":
                        continuation = "retry_node"
            _project_decomposed_runtime_manifest(state)
            TemporalTaskStateRepository(session).upsert(
                task_id=task_id, state=_serialize_temporal_task_state(state)
            )
            return NodeWaveMergeResult(
                continuation=continuation,
                blocked_node_id=blocked.node_id if blocked else None,
                blocked_logical_activity_key=blocked.logical_activity_key if blocked else None,
                requested_permission=blocked.result.requested_permission if blocked else None,
            ).model_dump(mode="json")

    @activity.defn(name="request_permission_escalation")
    @_restore_task_trace_context
    async def request_permission_escalation(self, task_id: str) -> None:
        """Persist the worker's higher-permission request before waiting."""

        def _persist() -> None:
            with session_scope(self.service.session_factory) as session:
                snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
                if snapshot is None:
                    raise RuntimeError(f"Task '{task_id}' has no Temporal state.")
                state = OrchestratorState.model_validate(snapshot.state)
                state = _rehydrate_dag_state(session, task_id, state, raw_snapshot=snapshot)
                result = state.result
                if result is None or result.next_action_hint != "request_higher_permission":
                    return
                requested = result.requested_permission or "unknown"
                blocked = next(
                    (
                        outcome
                        for outcome in state.node_outcomes
                        if outcome.status == "blocked"
                        and outcome.result.next_action_hint == "request_higher_permission"
                    ),
                    None,
                )
                data: dict[str, Any] = {
                    "source": "worker_permission_escalation",
                    "requested_permission": requested,
                    "resume_token": f"permission-escalation-{task_id}-{requested}",
                }
                if blocked is not None:
                    data.update(
                        {
                            "blocked_node_id": blocked.node_id,
                            "blocked_logical_activity_key": blocked.logical_activity_key,
                        }
                    )
                summary = result.summary or f"Worker requested higher permission: {requested}"
                decision_key = compute_interaction_content_hash(
                    HumanInteractionType.PERMISSION.value, summary, data
                )
                existing = (
                    session.query(HumanInteraction)
                    .filter_by(task_id=task_id, decision_key=decision_key)
                    .one_or_none()
                )
                if existing is None:
                    session.add(
                        HumanInteraction(
                            task_id=task_id,
                            interaction_type=HumanInteractionType.PERMISSION,
                            status=HumanInteractionStatus.PENDING,
                            hitl_mode=HumanInteractionHitlMode.REQUIRE_APPROVAL,
                            summary=summary,
                            decision_key=decision_key,
                            data=data,
                        )
                    )

        await self.service._run_blocking(_persist)

    @activity.defn(name="resolve_permission_escalation")
    @_restore_task_trace_context
    async def resolve_permission_escalation(self, task_id: str, approved: bool) -> None:
        """Apply a signalled escalation decision to durable task state."""
        await self.service._run_blocking(
            _resolve_permission_escalation_state,
            self.service.session_factory,
            task_id,
            approved,
        )

    @activity.defn(name="persist_rejected_session_state")
    @_restore_task_trace_context
    async def persist_rejected_session_state(self, task_id: str) -> None:
        """Persist compact state for an initial approval rejection."""

        def _persist() -> None:
            with session_scope(self.service.session_factory) as session:
                task = TaskRepository(session).get(task_id)
                snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
                if task is None or snapshot is None:
                    logger.warning(
                        "Cannot persist rejected compact session state without task snapshot",
                        extra={"task_id": task_id},
                    )
                    return
                state = OrchestratorState.model_validate(snapshot.state)
                state = _rehydrate_dag_state(session, task_id, state, raw_snapshot=snapshot)
                _persist_rejected_session_state(
                    session,
                    task,
                    state,
                    initial_approval_rejected=True,
                )
                TemporalTaskStateRepository(session).delete(task_id=task_id)

        await self.service._run_blocking(_persist)

    @activity.defn(name="record_workflow_failure")
    @_restore_task_trace_context
    async def record_workflow_failure(self, task_id: str, failure: str) -> None:
        """Project an exhausted Temporal activity failure into product state."""

        def _record_failure() -> None:
            with session_scope(self.service.session_factory) as session:
                task = TaskRepository(session).get(task_id)
                if task is None or task.status in (
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                ):
                    return
                task.status = TaskStatus.FAILED
                task.last_error = f"Temporal workflow failed: {failure}"
                TaskTimelineRepository(session).create_next_for_attempt(
                    task_id=task_id,
                    attempt_number=task.attempt_count,
                    event_type=TimelineEventType.TASK_FAILED,
                    event_key=f"temporal:{task_id}:workflow-failure",
                    message=task.last_error,
                )
                TemporalTaskStateRepository(session).delete(task_id=task_id)

        await self.service._run_blocking(_record_failure)

    @activity.defn(name="fail_node_permission_escalation")
    @_restore_task_trace_context
    async def fail_node_permission_escalation(self, task_id: str, node_id: str) -> None:
        """Project a global permission-cap failure onto the blocked plan node."""

        def _fail_node() -> None:
            with session_scope(self.service.session_factory) as session:
                plan = ExecutionPlanRepository(session).get_by_task_id(task_id)
                if plan is None:
                    raise RuntimeError(f"Task '{task_id}' has no execution plan.")
                node = ExecutionPlanRepository(session).get_node(plan.id, node_id)
                if node is None:
                    raise RuntimeError(f"Execution plan node '{node_id}' is unavailable.")
                ExecutionPlanRepository(session).update_node(
                    plan_id=plan.id,
                    node_id=node_id,
                    status=ExecutionPlanNodeStatus.FAILED,
                    failure_kind="permission_escalation_limit",
                    blocker_interaction_id=None,
                    finished_at=utc_now(),
                )

        await self.service._run_blocking(_fail_node)

    @activity.defn(name="verify_result")
    @_restore_task_trace_context
    async def verify_result(self, task_id: str) -> dict[str, Any]:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        has_prior_event = self._has_event(
            state,
            TimelineEventType.VERIFICATION_COMPLETED,
            TimelineEventType.VERIFICATION_SKIPPED,
        )
        if not verification_is_pending(state, has_prior_event=has_prior_event):
            logger.info("verify_result already executed for task %s, skipping", task_id)
            return decision_from_state(state).model_dump(mode="json")

        async def send_heartbeats() -> None:
            try:
                while True:
                    activity.heartbeat()
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except RuntimeError as exc:
                logger.debug("Temporal verification heartbeat failed for task %s: %s", task_id, exc)
                raise

        async def run_verification() -> OrchestratorState:
            state_dict = state.model_dump()
            for node in [self.verify_result_node, self.review_result_node]:
                updates = await self._run_node(node, state_dict)
                self._merge_updates(state_dict, updates)
            return OrchestratorState.model_validate(state_dict)

        started_at = utc_now()
        heartbeat_task = asyncio.create_task(
            send_heartbeats(), name=f"temporal-verification-heartbeat-{task_id}"
        )
        verification_task = asyncio.create_task(
            run_verification(), name=f"temporal-verification-{task_id}"
        )
        try:
            done, _ = await asyncio.wait(
                {verification_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat_task in done:
                heartbeat_error = heartbeat_task.exception()
                verification_task.cancel()
                await asyncio.gather(verification_task, return_exceptions=True)
                raise heartbeat_error or RuntimeError("Temporal verification heartbeat stopped.")
            state = await verification_task
        finally:
            if not verification_task.done():
                verification_task.cancel()
                await asyncio.gather(verification_task, return_exceptions=True)
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

        finished_at = utc_now()
        decision: CompletionLoopDecision = apply_verification_decision(state)

        await self.service._run_blocking(
            self._persist_intermediate_state,
            task_id=task_id,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
        )
        return decision.model_dump(mode="json")

    @activity.defn(name="deliver_result")
    @_restore_task_trace_context
    async def deliver_result(self, task_id: str) -> None:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        if self._has_event(
            state,
            TimelineEventType.TASK_COMPLETED,
            TimelineEventType.TASK_FAILED,
        ):
            await self.service._run_blocking(self._delete_temporal_snapshot, task_id)
            logger.info("deliver_result already executed for task %s, skipping", task_id)
            return

        started_at = utc_now()
        state_dict = state.model_dump()
        for node in [self.deliver_result_node, summarize_result]:
            updates = await self._run_node(node, state_dict)
            self._merge_updates(state_dict, updates)

        state = OrchestratorState.model_validate(state_dict)
        finished_at = utc_now()

        force_status = None
        if state.verification is not None and state.verification.status == "failed":
            # Verification is the final acceptance gate. A worker can report success
            # while deterministic validation finds a missing or invalid deliverable.
            force_status = TaskStatus.FAILED
        elif state.result is not None:
            force_status = (
                TaskStatus.COMPLETED if state.result.status == "success" else TaskStatus.FAILED
            )

        await self.service._run_blocking(
            self._persist_state,
            task_id=task_id,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
            force_status=force_status,
        )
        phase: ProgressPhase = (
            "completed"
            if state.result is not None and state.result.status == "success"
            else "failed"
        )
        await self._notify_progress(
            task_id,
            phase=phase,
            summary=state.result.summary if state.result is not None else None,
        )

    @activity.defn(name="persist_memory")
    @_restore_task_trace_context
    async def persist_memory(self, task_id: str) -> None:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        if self._has_event(state, TimelineEventType.MEMORY_PERSISTED):
            logger.info("persist_memory already executed for task %s, skipping", task_id)
            return

        started_at = utc_now()
        state_dict = state.model_dump()
        updates = await self._run_node(self.persist_memory_node, state_dict)
        self._merge_updates(state_dict, updates)

        state = OrchestratorState.model_validate(state_dict)
        finished_at = utc_now()

        await self.service._run_blocking(
            self._persist_intermediate_state,
            task_id=task_id,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
        )
