from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from functools import wraps
from typing import Any

from temporalio import activity

from apps.observability import with_restored_trace_context
from db.base import utc_now
from db.enums import (
    HumanInteractionHitlMode,
    HumanInteractionStatus,
    HumanInteractionType,
    TaskStatus,
    TimelineEventType,
)
from db.models import HumanInteraction
from db.utils import compute_interaction_content_hash
from orchestrator.execution_graph_input import build_orchestrator_graph_input
from orchestrator.execution_policy import _apply_execution_budget_policy
from orchestrator.graph import (
    build_await_result_node,
    build_decompose_task_node,
    build_generate_task_spec_and_route_node,
    build_load_memory_node,
    build_persist_memory_node,
    build_review_result_node,
    check_approval,
    summarize_result,
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
from orchestrator.state import OrchestratorState
from orchestrator.temporal.queues import execution_task_queue_for_profile
from repositories import (
    TaskRepository,
    TaskTimelineRepository,
    TemporalTaskStateRepository,
    session_scope,
)

logger = logging.getLogger(__name__)


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

    def _get_current_state(self, task_id: str) -> OrchestratorState:
        with session_scope(self.service.session_factory) as session:
            snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
            if snapshot is not None:
                state = OrchestratorState.model_validate(snapshot.state)
                task = TaskRepository(session).get(task_id)
                approval_data = (
                    (task.constraints or {}).get("approval") if task is not None else None
                )
                approval_status = (
                    approval_data.get("status") if isinstance(approval_data, dict) else None
                )
                if approval_status in {"approved", "rejected"} and state.approval.required:
                    state.approval = state.approval.model_copy(update={"status": approval_status})
                persisted_count = TaskTimelineRepository(session).count_by_attempt(
                    task_id=task_id,
                    attempt_number=state.attempt_count,
                )
                # Operator actions can append timeline events while a workflow is
                # paused. Reconcile the durable snapshot with that product-side
                # event before a resumed activity emits its next event.
                state.timeline_persisted_count = max(
                    state.timeline_persisted_count,
                    persisted_count,
                )
                return state

        loaded = self.service._load_submission_for_task(task_id=task_id)
        if not loaded:
            raise RuntimeError(f"Task {task_id} not found")
        submission, persisted = loaded

        def _get_count() -> int:
            with session_scope(self.service.session_factory) as session:
                return TaskTimelineRepository(session).count_by_attempt(
                    task_id=task_id,
                    attempt_number=persisted.attempt_count,
                )

        timeline_persisted_count = _get_count()
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
        return OrchestratorState.model_validate(graph_input)

    def _load_task_trace_context(self, task_id: str) -> dict[str, str]:
        with session_scope(self.service.session_factory) as session:
            task = TaskRepository(session).get(task_id)
            return dict(task.trace_context or {}) if task is not None else {}

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
        with session_scope(self.service.session_factory) as session:
            TemporalTaskStateRepository(session).delete(task_id=task_id)

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
            if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                task.status = force_status or TaskStatus.IN_PROGRESS
            _persist_timeline_events(session, task_id, state)
            # Snapshot the cursor together with the events. The next activity
            # restores this state and must not try to insert the same timeline
            # rows again.
            state.timeline_persisted_count = len(state.timeline_events)
            if clear_snapshot:
                TemporalTaskStateRepository(session).delete(task_id=task_id)
            else:
                TemporalTaskStateRepository(session).upsert(
                    task_id=task_id, state=state.model_dump(mode="json")
                )

    def _merge_updates(self, state_dict: dict[str, Any], updates: dict[str, Any]) -> None:
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
        else:

            def _sync_run() -> Any:
                if hasattr(node, "invoke"):
                    return node.invoke(state_dict)
                return node(state_dict)

            res = await self.service._run_blocking(_sync_run)

        import inspect

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
                    state.route.chosen_profile
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
        state.timeline_persisted_count = len(state.timeline_events)

        return {
            "requires_clarification": bool(
                state.task_spec and state.task_spec.requires_clarification
            ),
            "requires_approval": state.approval.required if state.approval else False,
            "execution_task_queue": execution_task_queue_for_profile(state.route.chosen_profile),
        }

    @activity.defn(name="decompose_task")
    @_restore_task_trace_context
    async def decompose_task(self, task_id: str) -> None:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        if self._has_event(state, TimelineEventType.TASK_PLANNED):
            logger.info("decompose_task already executed for task %s, skipping", task_id)
            return

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
        state.timeline_persisted_count = len(state.timeline_events)

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
        state.timeline_persisted_count = len(state.timeline_events)

    @activity.defn(name="provision_workspace")
    @_restore_task_trace_context
    async def provision_workspace(self, task_id: str) -> None:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        retrying_permission_escalation = bool(
            state.task.constraints.get("permission_escalation_retry")
        )
        if (
            self._has_event(state, TimelineEventType.WORKSPACE_PROVISIONED)
            and not retrying_permission_escalation
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
        state.timeline_persisted_count = len(state.timeline_events)

    @activity.defn(name="run_worker")
    @_restore_task_trace_context
    async def run_worker(self, task_id: str) -> dict[str, bool]:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        retrying_permission_escalation = bool(
            state.task.constraints.get("permission_escalation_retry")
        )
        if (
            self._has_event(
                state,
                TimelineEventType.WORKER_COMPLETED,
                TimelineEventType.WORKER_FAILED,
                TimelineEventType.WORKER_ERROR,
            )
            and not retrying_permission_escalation
        ):
            logger.info("run_worker already executed for task %s, skipping", task_id)
            return {"requires_permission_escalation": False}

        async def send_heartbeats() -> None:
            while True:
                activity.heartbeat()
                await asyncio.sleep(5)

        heartbeat_task = asyncio.create_task(
            send_heartbeats(), name=f"temporal-worker-heartbeat-{task_id}"
        )
        try:
            started_at = utc_now()
            state_dict = state.model_dump()
            updates = await self._run_node(self.await_result_node, state_dict)
            self._merge_updates(state_dict, updates)

            state = OrchestratorState.model_validate(state_dict)
            constraints = dict(state.task.constraints)
            constraints.pop("permission_escalation_retry", None)
            state.task = state.task.model_copy(update={"constraints": constraints})
            finished_at = utc_now()

            await self.service._run_blocking(
                self._persist_intermediate_state,
                task_id=task_id,
                state=state,
                started_at=started_at,
                finished_at=finished_at,
            )
            state.timeline_persisted_count = len(state.timeline_events)
            return {
                "requires_permission_escalation": bool(
                    state.result and state.result.next_action_hint == "request_higher_permission"
                )
            }
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

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
                result = state.result
                if result is None or result.next_action_hint != "request_higher_permission":
                    return
                requested = result.requested_permission or "unknown"
                data = {
                    "source": "worker_permission_escalation",
                    "requested_permission": requested,
                    "resume_token": f"permission-escalation-{task_id}-{requested}",
                }
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

        def _resolve() -> None:
            with session_scope(self.service.session_factory) as session:
                task = TaskRepository(session).get(task_id)
                snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
                if task is None or snapshot is None:
                    raise RuntimeError(
                        f"Task '{task_id}' is unavailable for permission escalation."
                    )
                state = OrchestratorState.model_validate(snapshot.state)
                requested = state.result.requested_permission if state.result else None
                if not approved:
                    task.status = TaskStatus.FAILED
                    task.last_error = "Worker permission escalation rejected by operator."
                    task.next_attempt_at = None
                    TaskTimelineRepository(session).create_next_for_attempt(
                        task_id=task_id,
                        attempt_number=task.attempt_count,
                        event_type=TimelineEventType.APPROVAL_REJECTED,
                        event_key=f"permission-escalation:{task_id}:rejected",
                        message=task.last_error,
                    )
                    TemporalTaskStateRepository(session).delete(task_id=task_id)
                    return
                constraints = dict(task.constraints or {})
                constraints["granted_permission"] = requested
                constraints["permission_escalation_retry"] = True
                task.constraints = constraints
                task.status = TaskStatus.IN_PROGRESS
                state.task = state.task.model_copy(update={"constraints": constraints})
                state.result = None
                TemporalTaskStateRepository(session).upsert(
                    task_id=task_id, state=state.model_dump(mode="json")
                )

        await self.service._run_blocking(_resolve)

    @activity.defn(name="record_workflow_failure")
    @_restore_task_trace_context
    async def record_workflow_failure(self, task_id: str, failure: str) -> None:
        """Project an exhausted Temporal activity failure into product state."""

        def _record_failure() -> None:
            with session_scope(self.service.session_factory) as session:
                task = TaskRepository(session).get(task_id)
                if task is None or task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    return
                task.status = TaskStatus.FAILED
                task.last_error = f"Temporal workflow failed: {failure}"
                task.next_attempt_at = None
                TaskTimelineRepository(session).create_next_for_attempt(
                    task_id=task_id,
                    attempt_number=task.attempt_count,
                    event_type=TimelineEventType.TASK_FAILED,
                    event_key=f"temporal:{task_id}:workflow-failure",
                    message=task.last_error,
                )
                TemporalTaskStateRepository(session).delete(task_id=task_id)

        await self.service._run_blocking(_record_failure)

    @activity.defn(name="verify_result")
    @_restore_task_trace_context
    async def verify_result(self, task_id: str) -> None:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        if self._has_event(
            state,
            TimelineEventType.VERIFICATION_COMPLETED,
            TimelineEventType.VERIFICATION_SKIPPED,
        ):
            logger.info("verify_result already executed for task %s, skipping", task_id)
            return

        started_at = utc_now()
        state_dict = state.model_dump()
        for node in [self.verify_result_node, self.review_result_node]:
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
        state.timeline_persisted_count = len(state.timeline_events)

    @activity.defn(name="deliver_result")
    @_restore_task_trace_context
    async def deliver_result(self, task_id: str) -> None:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        if self._has_event(
            state,
            TimelineEventType.TASK_COMPLETED,
            TimelineEventType.TASK_FAILED,
        ):
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
        state.timeline_persisted_count = len(state.timeline_events)

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
        state.timeline_persisted_count = len(state.timeline_events)
