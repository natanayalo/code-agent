from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from datetime import datetime
from functools import wraps
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
from orchestrator.graph import (
    _aggregate_decomposed_results,
    _await_worker_with_timeout,
    _build_worker_request,
    _effective_input_evidence,
    _resolve_orchestrator_timeout_seconds,
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
from orchestrator.state import NodeOutcome, OrchestratorState
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
    TaskRepository,
    TaskTimelineRepository,
    TemporalTaskStateRepository,
    session_scope,
)
from sandbox.scratch import scratch_namespace_component
from workers import WorkerResult

logger = logging.getLogger(__name__)

EXECUTION_CAPACITY_LEASE_SECONDS = 60


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
                merged_keys = {
                    outcome.logical_activity_key
                    for outcome in state.node_outcomes
                    if outcome.logical_activity_key
                }
                outcomes = {outcome.node_id: outcome for outcome in state.node_outcomes}
                has_pending_node = False
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
                                "replayed": (
                                    selection.action == "merge_terminal"
                                    and persisted_node.node_id == node.node_id
                                ),
                            }
                        )
                    )
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
                ExecutionPlanRepository(session).update_node(
                    plan_id=plan.id,
                    node_id=item.node_id,
                    status=ExecutionPlanNodeStatus.FAILED,
                    failure_kind="sandbox_infra",
                    finished_at=utc_now(),
                )
            outcomes: list[NodeOutcome] = []
            for node in plan.nodes:
                if node.terminal_result_payload:
                    outcome = NodeOutcome.model_validate(
                        node.terminal_result_payload["node_outcome"]
                    )
                    outcomes.append(
                        outcome.model_copy(
                            update={
                                "dependencies": list(node.depends_on or []),
                                "logical_activity_key": node.latest_logical_activity_key,
                                "result_digest": node.terminal_result_digest,
                            }
                        )
                    )
                elif node.node_id in missing_evidence:
                    selected = next(
                        item for item in selection.items if item.node_id == node.node_id
                    )
                    outcomes.append(
                        NodeOutcome(
                            node_id=node.node_id,
                            status="failed",
                            result=WorkerResult(
                                status="failure",
                                failure_kind="sandbox_infra",
                                summary=(
                                    "Fan-out activity ended without durable terminal evidence."
                                ),
                            ),
                            dependencies=list(node.depends_on or []),
                            attempts=selected.activity_request.logical_attempt,
                            logical_activity_key=selected.activity_request.logical_activity_key,
                        )
                    )
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
            TemporalTaskStateRepository(session).upsert(
                task_id=task_id, state=state.model_dump(mode="json")
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
                    # Retain the terminal key in parent state while the node is
                    # reset for its next logical attempt. Otherwise selection
                    # would replay the old blocked payload before it can run.
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
