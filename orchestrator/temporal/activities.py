from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from datetime import datetime
from functools import wraps
from typing import Any

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
from db.models import ExecutionPlanNodeAttempt, HumanInteraction
from db.utils import compute_interaction_content_hash
from orchestrator.execution_graph_input import build_orchestrator_graph_input
from orchestrator.execution_policy import _apply_execution_budget_policy
from orchestrator.graph import (
    _aggregate_decomposed_results,
    _build_worker_request,
    _effective_input_evidence,
    _skipped_node_result,
    build_await_result_node,
    build_decompose_task_node,
    build_generate_task_spec_and_route_node,
    build_load_memory_node,
    build_persist_memory_node,
    build_review_result_node,
    check_approval,
    summarize_result,
)
from orchestrator.node_execution import (
    NodeActivityClaimLost,
    NodeActivityInProgress,
    NodeActivityRequest,
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
from orchestrator.state import NodeOutcome, OrchestratorState
from orchestrator.temporal.node_wave import (
    DecomposeTaskResult,
    NodeSelectionResult,
    NodeWaveMergeRequest,
    NodeWaveMergeResult,
)
from orchestrator.temporal.queues import execution_task_queue_for_profile
from repositories import (
    ExecutionPlanRepository,
    TaskRepository,
    TaskTimelineRepository,
    TemporalTaskStateRepository,
    session_scope,
)
from workers import WorkerResult

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
                if (
                    approval_status in {"approved", "rejected"}
                    and state.approval is not None
                    and state.approval.required
                ):
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
        state.timeline_persisted_count = len(state.timeline_events)

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
        if self._has_event(state, TimelineEventType.TASK_PLANNED):
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
        state.timeline_persisted_count = len(state.timeline_events)
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
        state.timeline_persisted_count = len(state.timeline_events)

    @activity.defn(name="provision_workspace")
    @_restore_task_trace_context
    async def provision_workspace(self, task_id: str) -> None:
        state = await self.service._run_blocking(self._get_current_state, task_id)
        task_constraints = (
            state.task.constraints if isinstance(state.task.constraints, dict) else {}
        )
        retrying_permission_escalation = bool(task_constraints.get("permission_escalation_retry"))
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
        task_constraints = (
            state.task.constraints if isinstance(state.task.constraints, dict) else {}
        )
        retrying_permission_escalation = bool(task_constraints.get("permission_escalation_retry"))
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
            try:
                while True:
                    activity.heartbeat()
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except RuntimeError as exc:
                logger.debug("Temporal heartbeat failed for task %s: %s", task_id, exc)
                raise

        heartbeat_task = asyncio.create_task(
            send_heartbeats(), name=f"temporal-worker-heartbeat-{task_id}"
        )
        worker_task: asyncio.Task[dict[str, Any]] | None = None
        try:
            started_at = utc_now()
            state_dict = state.model_dump()
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

            state = OrchestratorState.model_validate(state_dict)
            constraints = state.task.constraints if isinstance(state.task.constraints, dict) else {}
            constraints = dict(constraints)
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
            if worker_task is not None and not worker_task.done():
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    @activity.defn(name="select_next_node")
    @_restore_task_trace_context
    async def select_next_node(self, task_id: str) -> dict[str, Any]:
        """Read durable state and choose one deterministic node-wave action."""
        state = await self.service._run_blocking(self._get_current_state, task_id)
        if state.decomposed_plan is None or state.decomposed_plan.status != "decomposed":
            return NodeSelectionResult(
                action="invalid", reason="Task is not decomposed."
            ).model_dump(mode="json")

        def _select() -> NodeSelectionResult:
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
                merged_keys = {
                    outcome.logical_activity_key
                    for outcome in state.node_outcomes
                    if outcome.logical_activity_key
                }
                outcomes = {outcome.node_id: outcome for outcome in state.node_outcomes}
                for node in plan.nodes:
                    if (
                        node.latest_logical_activity_key
                        and node.terminal_result_payload
                        and node.latest_logical_activity_key not in merged_keys
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
                    dependencies = list(node.depends_on or [])
                    unresolved = [
                        dependency for dependency in dependencies if dependency not in outcomes
                    ]
                    if unresolved:
                        continue
                    failed = [
                        dependency
                        for dependency in dependencies
                        if outcomes[dependency].status != "completed"
                    ]
                    if failed:
                        return NodeSelectionResult(
                            action="skip", node_id=node.node_id, failed_dependency_ids=failed
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
                    request = NodeActivityRequest(
                        task_id=task_id,
                        plan_id=plan.id,
                        node_id=node.node_id,
                        logical_attempt=logical_attempt,
                        logical_activity_key=logical_activity_key(
                            plan.id, node.node_id, logical_attempt
                        ),
                        effective_input_digest=digest,
                    )
                    return NodeSelectionResult(
                        action="execute",
                        activity_request=request,
                        execution_task_queue=execution_task_queue_for_profile(
                            state.route.chosen_profile if state.route else None
                        ),
                        node_id=node.node_id,
                        logical_activity_key=request.logical_activity_key,
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
        dependencies = [outcome_by_id[dependency] for dependency in node.depends_on]
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
        evidence, digest = _effective_input_evidence(state, node, prior_context)
        if digest != node_activity.effective_input_digest:
            raise ValueError("Node activity input digest changed before execution.")

        async def _execute_worker() -> Any:
            result = await self.service.worker.run(request)
            return (
                result
                if result is not None
                else WorkerResult(
                    status="failure",
                    summary="Node worker returned no result.",
                    failure_kind="worker_failure",
                )
            )

        async def _heartbeat() -> None:
            while True:
                activity.heartbeat()
                await asyncio.sleep(5)

        heartbeat = asyncio.create_task(_heartbeat())
        try:
            result_ref, _outcome = await NodeExecutionService(self.service.session_factory).execute(
                activity=node_activity,
                request=request,
                effective_input_summary=evidence,
                execute_worker=_execute_worker,
            )
            return result_ref.model_dump(mode="json")
        except (NodeActivityInProgress, NodeActivityClaimLost):
            raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    @activity.defn(name="merge_node_wave")
    @_restore_task_trace_context
    async def merge_node_wave(self, task_id: str, merge_data: dict[str, Any]) -> dict[str, Any]:
        """Validate durable node evidence and atomically project the parent state."""
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
                    item for item in state.decomposed_plan.nodes if item.node_id == node.node_id
                )
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
                    attempt = session.scalar(
                        select(ExecutionPlanNodeAttempt).where(
                            ExecutionPlanNodeAttempt.plan_node_id == node.id,
                            ExecutionPlanNodeAttempt.logical_activity_key == key,
                        )
                    )
                    if attempt is None or not attempt.result_payload or not attempt.result_digest:
                        raise ValueError("Terminal node result is unavailable for merge.")
                    expected = (
                        merge.result_ref.result_digest
                        if merge.result_ref
                        else selection.result_digest
                    )
                    if expected and expected != attempt.result_digest:
                        raise ValueError("Node result digest does not match durable evidence.")
                    WorkerResult.model_validate(attempt.result_payload["worker_result"])
                    NodeOutcome.model_validate(attempt.result_payload["node_outcome"])
                    digest = attempt.result_digest

                outcomes: list[NodeOutcome] = []
                for persisted_node in plan.nodes:
                    persisted_payload = persisted_node.terminal_result_payload
                    if not persisted_payload:
                        continue
                    outcome = NodeOutcome.model_validate(persisted_payload["node_outcome"])
                    outcomes.append(
                        outcome.model_copy(
                            update={
                                "dependencies": list(persisted_node.depends_on or []),
                                "logical_activity_key": persisted_node.latest_logical_activity_key,
                                "result_digest": persisted_node.terminal_result_digest,
                                "replayed": selection.action == "merge_terminal",
                            }
                        )
                    )
                state.node_outcomes = outcomes
                state.result = _aggregate_decomposed_results(outcomes)
                current = next(outcome for outcome in outcomes if outcome.node_id == node.node_id)
                if current.status == "blocked":
                    return NodeWaveMergeResult(
                        continuation="await_permission",
                        blocked_node_id=node.node_id,
                        blocked_logical_activity_key=key,
                        requested_permission=current.result.requested_permission,
                    )
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
        await self.service._run_blocking(
            self._persist_intermediate_state,
            task_id=task_id,
            state=state,
            started_at=utc_now(),
            finished_at=utc_now(),
        )
        return result.model_dump(mode="json")

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
                blocked = next(
                    (
                        outcome
                        for outcome in state.node_outcomes
                        if outcome.status == "blocked"
                        and outcome.result.next_action_hint == "request_higher_permission"
                    ),
                    None,
                )
                plan = ExecutionPlanRepository(session).get_by_task_id(task_id)
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
                    if blocked is not None and plan is not None:
                        ExecutionPlanRepository(session).update_node(
                            plan_id=plan.id,
                            node_id=blocked.node_id,
                            status=ExecutionPlanNodeStatus.FAILED,
                            failure_kind="permission_denied",
                        )
                    TemporalTaskStateRepository(session).delete(task_id=task_id)
                    return
                constraints = dict(task.constraints or {})
                constraints["granted_permission"] = requested
                constraints["permission_escalation_retry"] = True
                task.constraints = constraints
                task.status = TaskStatus.IN_PROGRESS
                state.task = state.task.model_copy(update={"constraints": constraints})
                if blocked is not None and plan is not None:
                    ExecutionPlanRepository(session).update_node(
                        plan_id=plan.id,
                        node_id=blocked.node_id,
                        status=ExecutionPlanNodeStatus.PENDING,
                        blocker_interaction_id=None,
                        retry_count=blocked.attempts,
                    )
                    state.node_outcomes = [
                        outcome
                        for outcome in state.node_outcomes
                        if outcome.node_id != blocked.node_id
                    ]
                    state.result = _aggregate_decomposed_results(state.node_outcomes)
                else:
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
