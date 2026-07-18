"""Execution-path persistence service for the T-044 HTTP vertical slice."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from anyio import to_thread
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.orm import Session, sessionmaker

from apps.observability import (
    bind_current_trace_context,
)
from apps.observability import (
    with_restored_trace_context as _with_restored_trace_context,
)
from apps.runtime import execution_runtime
from db.enums import TaskStatus
from db.models import Task as _Task
from orchestrator import (
    execution_heartbeat_service,
    execution_improvement_proposal_service,
    execution_interaction_service,
    execution_outcome_service,
    execution_proposal_service,
    execution_retention_service,
    execution_runtime_service,
    execution_snapshot_service,
    execution_submission_service,
    execution_worker_service,
)
from orchestrator import (
    execution_policy as _execution_policy_module,
)
from orchestrator.brain import OrchestratorBrain
from orchestrator.checkpoints import create_async_sqlite_checkpointer
from orchestrator.execution_policy import (
    _apply_execution_budget_policy,
    _deep_merge,
    _heartbeat_interval_seconds,
    _sanitize_submission_constraints,
    _validate_callback_url,
    shutdown_callback_dns_executor,
    validate_callback_url,
)
from orchestrator.execution_queue import TaskQueueWorker
from orchestrator.execution_serialization import (
    _approval_constraints_payload,
    _artifact_type_for_persistence,
    _completion_progress_phase,
    _enum_value,
    _extract_graph_payload,
    _get_trace_id_from_context,
    _interrupt_payload_from_object,
    _interrupt_summary,
    _normalize_orchestrator_graph_output,
    _requires_manual_follow_up,
    _review_result_artifact_entry,
    _serialize_review_result,
    _serialize_verification_report,
    _summarize_graph_span_input,
    _summarize_graph_span_output,
    _terminal_follow_up_status,
    _to_json_compatible,
    _workspace_id_from_artifacts,
)
from orchestrator.execution_tracing import (
    _clear_tracing_config_cache,
    _get_phoenix_url,
    _get_project_id,
    _get_tracing_config,
    bootstrap_phoenix_project_id,
)
from orchestrator.execution_types import (
    ApprovalDecisionResult,
    ArtifactSnapshot,
    CreateTaskOutcome,
    DeliveryKey,
    ExecutionModel,
    HumanInteractionSnapshot,
    InteractionInboxCard,
    InteractionResponse,
    OperationalMetrics,
    PersonalMemorySnapshot,
    PersonalMemoryUpsertRequest,
    ProgressEvent,
    ProgressNotifier,
    ProgressPhase,
    ProjectMemorySnapshot,
    ProjectMemoryUpsertRequest,
    ProposalSnapshot,
    SessionSnapshot,
    SessionWorkingContextSnapshot,
    SubmissionSession,
    TaskApprovalDecision,
    TaskClaim,
    TaskReplayRequest,
    TaskReplayResult,
    TaskSnapshot,
    TaskSubmission,
    TaskSubmissionValidationError,
    TaskSummarySnapshot,
    TaskTimelineEventSnapshot,
    WorkerRunSnapshot,
    _PersistedTaskContext,
)
from orchestrator.graph import build_orchestrator_graph
from orchestrator.improvement_suggestions import ImprovementSuggestionScorer
from sandbox import WorkspaceManager
from workers import Worker, WorkerProfile

logger = logging.getLogger(__name__)


class TemporalUnavailableError(RuntimeError):
    """Raised when Temporal cannot accept a new task submission."""


with_restored_trace_context = _with_restored_trace_context
socket = _execution_policy_module.socket
_task_status_from_result = _execution_policy_module._task_status_from_result
_worker_run_status_from_result = _execution_policy_module._worker_run_status_from_result
_worker_type_for_persistence = _execution_policy_module._worker_type_for_persistence
Task = _Task


class TaskExecutionService:
    """Submit tasks through the orchestrator and persist execution-path state."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        worker: Worker,
        worker_profiles: Mapping[str, WorkerProfile] | None = None,
        enable_worker_profiles: bool = False,
        enable_independent_verifier: bool = False,
        orchestrator_brain: OrchestratorBrain | None = None,
        improvement_scorer: ImprovementSuggestionScorer | None = None,
        enable_improvement_llm_scoring: bool = False,
        progress_notifier: ProgressNotifier | None = None,
        default_task_max_attempts: int = 3,
        workspace_root: str | Path | None = None,
        retention_seconds: int | None = 7 * 24 * 60 * 60,
        checkpoint_path: str | Path | None = None,
        decomposed_fanout_enabled: bool = False,
        enforce_temporal_availability: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.worker = worker
        self.worker_profiles = dict(worker_profiles or {})
        self.enable_worker_profiles = enable_worker_profiles
        self.enable_independent_verifier = enable_independent_verifier
        self.orchestrator_brain = orchestrator_brain
        self.improvement_scorer = improvement_scorer
        self.enable_improvement_llm_scoring = enable_improvement_llm_scoring
        self.progress_notifier = progress_notifier
        self.default_task_max_attempts = max(1, default_task_max_attempts)
        self.workspace_root = None
        if workspace_root is not None:
            self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.workspace_manager = (
            WorkspaceManager(self.workspace_root) if self.workspace_root else None
        )
        self.retention_seconds = None if retention_seconds is None else max(0, retention_seconds)
        self.checkpoint_path = checkpoint_path
        # Read once at service construction; Temporal workflows replay the
        # decision returned by selection rather than consulting process state.
        self.decomposed_fanout_enabled = decomposed_fanout_enabled
        self.enforce_temporal_availability = enforce_temporal_availability
        self._checkpointer: BaseCheckpointSaver | None = None
        self._checkpointer_cm: AbstractAsyncContextManager[BaseCheckpointSaver] | None = None
        self._graph: Any | None = None
        self._temporal_clients: dict[asyncio.AbstractEventLoop, Any] = {}
        self._temporal_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        self._temporal_cache_lock = threading.Lock()

    @property
    def graph(self) -> Any:
        """Lazy-loaded orchestrator graph, compiled with the current checkpointer."""
        if self._graph is None:
            self._graph = build_orchestrator_graph(
                worker=self.worker,
                workspace_manager=self.workspace_manager,
                worker_profiles=self.worker_profiles,
                enable_worker_profiles=self.enable_worker_profiles,
                enable_independent_verifier=self.enable_independent_verifier,
                orchestrator_brain=self.orchestrator_brain,
                session_factory=self.session_factory,
                checkpointer=self._checkpointer,
            )
        return self._graph

    async def __aenter__(self) -> TaskExecutionService:
        """Initialize shared resources (like checkpointers) if configured."""
        if self.checkpoint_path and not self._checkpointer:
            self._checkpointer_cm = create_async_sqlite_checkpointer(self.checkpoint_path)
            self._checkpointer = await self._checkpointer_cm.__aenter__()
            # Invalidate graph to force recompile with checkpointer
            self._graph = None
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Close shared resources."""
        if self._checkpointer_cm:
            await self._checkpointer_cm.__aexit__(exc_type, exc_val, exc_tb)
            self._checkpointer = None
            self._checkpointer_cm = None
            # Invalidate graph to revert to memory-only
            self._graph = None

    _workspace_path_for_run = execution_retention_service._workspace_path_for_run
    _delete_retained_workspace_path = execution_retention_service._delete_retained_workspace_path
    _prune_retained_runs = execution_retention_service._prune_retained_runs

    def create_task(self, submission: TaskSubmission) -> tuple[TaskSnapshot, _PersistedTaskContext]:
        """Persist a new task request and return the initial pollable snapshot."""
        outcome = self.create_task_outcome(submission)
        if outcome.persisted is None:
            raise RuntimeError("Expected a fresh task, but a duplicate delivery was returned.")
        return outcome.task_snapshot, outcome.persisted

    def create_task_outcome(
        self,
        submission: TaskSubmission,
        *,
        delivery_key: DeliveryKey | None = None,
    ) -> CreateTaskOutcome:
        """Persist a task request or return the previously created task for a duplicate delivery."""
        submission = self._normalize_and_validate_submission(submission)
        if delivery_key is not None and delivery_key.channel != submission.session.channel:
            raise ValueError(
                "delivery_key.channel must match submission.session.channel for dedupe."
            )
        if delivery_key is not None:
            from repositories import InboundDeliveryRepository, session_scope

            with session_scope(self.session_factory) as session:
                existing = InboundDeliveryRepository(session).get_by_channel_delivery(
                    channel=delivery_key.channel, delivery_id=delivery_key.delivery_id
                )
                if existing is not None and existing.task_id is not None:
                    snapshot = self.get_task(existing.task_id)
                    if snapshot is None:
                        raise RuntimeError("Inbound delivery references a missing task.")
                    logger.warning(
                        "Duplicate task delivery detected and deduplicated",
                        extra={
                            "task_id": existing.task_id,
                            "delivery_id": delivery_key.delivery_id,
                            "channel": delivery_key.channel,
                        },
                    )
                    return CreateTaskOutcome(task_snapshot=snapshot, persisted=None, duplicate=True)
        if self.enforce_temporal_availability:
            self.ensure_temporal_available()
        persisted, duplicate_task_id = self._persist_submission(
            submission,
            status=TaskStatus.PENDING,
            max_attempts=self.default_task_max_attempts,
            delivery_key=delivery_key,
        )
        task_id = duplicate_task_id or (persisted.task_id if persisted is not None else None)
        if task_id is None:
            raise RuntimeError("Task persistence did not produce a task id.")

        task_snapshot = self.get_task(task_id)
        if task_snapshot is None:
            raise RuntimeError(f"Persisted task '{task_id}' could not be reloaded.")
        outcome = CreateTaskOutcome(
            task_snapshot=task_snapshot,
            persisted=persisted,
            duplicate=duplicate_task_id is not None,
        )
        if outcome.duplicate:
            logger.warning(
                "Duplicate task delivery detected and deduplicated",
                extra={
                    "task_id": task_id,
                    "delivery_id": delivery_key.delivery_id if delivery_key else None,
                    "channel": delivery_key.channel if delivery_key else None,
                },
            )
        return outcome

    def ensure_temporal_available(self) -> None:
        """Fail new submissions unless the Temporal SDK completes its readiness RPC."""
        if execution_runtime() != "temporal":
            return
        temporal_address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                from temporalio.client import Client

                asyncio.run(Client.connect(temporal_address))
                return
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.1 * (2**attempt))
        raise TemporalUnavailableError(
            f"Temporal is unavailable at {temporal_address}; new tasks are temporarily disabled."
        ) from last_error

    _normalize_and_validate_submission = (
        execution_submission_service._normalize_and_validate_submission
    )
    submit_task = execution_runtime_service.submit_task
    run_queued_task = execution_runtime_service.run_queued_task
    _update_span_status_from_state = execution_runtime_service._update_span_status_from_state
    _record_execution_span_error = execution_runtime_service._record_execution_span_error
    _heartbeat_loop = execution_heartbeat_service._heartbeat_loop
    _heartbeat_task_and_worker = execution_heartbeat_service._heartbeat_task_and_worker
    claim_next_task = execution_snapshot_service.claim_next_task
    is_execution_busy = execution_snapshot_service.is_execution_busy
    get_task = execution_snapshot_service.get_task
    list_tasks = execution_snapshot_service.list_tasks
    list_sessions = execution_snapshot_service.list_sessions
    get_session = execution_snapshot_service.get_session
    get_knowledge_base_stats = execution_snapshot_service.get_knowledge_base_stats
    list_personal_memory = execution_snapshot_service.list_personal_memory
    search_personal_memory = execution_snapshot_service.search_personal_memory
    upsert_personal_memory = execution_snapshot_service.upsert_personal_memory
    delete_personal_memory = execution_snapshot_service.delete_personal_memory
    list_project_memory = execution_snapshot_service.list_project_memory
    search_project_memory = execution_snapshot_service.search_project_memory
    upsert_project_memory = execution_snapshot_service.upsert_project_memory
    delete_project_memory = execution_snapshot_service.delete_project_memory
    create_memory_proposal = execution_snapshot_service.create_memory_proposal
    list_memory_proposals = execution_snapshot_service.list_memory_proposals
    list_memory_observations = execution_snapshot_service.list_memory_observations
    get_memory_observation = execution_snapshot_service.get_memory_observation
    list_memory_admission_decisions = execution_snapshot_service.list_memory_admission_decisions
    accept_memory_proposal = execution_snapshot_service.accept_memory_proposal
    reject_memory_proposal = execution_snapshot_service.reject_memory_proposal
    _map_task_to_snapshot = execution_snapshot_service._map_task_to_snapshot
    _map_task_to_summary = execution_snapshot_service._map_task_to_summary
    _map_to_snapshot = execution_proposal_service._map_to_snapshot
    list_proposals = execution_proposal_service.list_proposals
    accept_proposal = execution_proposal_service.accept_proposal
    reject_proposal = execution_proposal_service.reject_proposal
    _is_pending_interaction = staticmethod(execution_snapshot_service._is_pending_interaction)
    _map_human_interaction_snapshot = staticmethod(
        execution_snapshot_service._map_human_interaction_snapshot
    )
    _pending_interaction_snapshots = execution_snapshot_service._pending_interaction_snapshots
    _count_pending_interactions = execution_snapshot_service._count_pending_interactions
    _ensure_verifier_outcome_ids = execution_snapshot_service._ensure_verifier_outcome_ids
    _map_session_to_snapshot = execution_snapshot_service._map_session_to_snapshot
    _map_personal_memory_to_snapshot = staticmethod(
        execution_snapshot_service._map_personal_memory_to_snapshot
    )
    _map_project_memory_to_snapshot = staticmethod(
        execution_snapshot_service._map_project_memory_to_snapshot
    )
    _map_memory_proposal_to_snapshot = staticmethod(
        execution_snapshot_service._map_memory_proposal_to_snapshot
    )
    _map_memory_observation_to_snapshot = staticmethod(
        execution_snapshot_service._map_memory_observation_to_snapshot
    )
    _map_memory_admission_decision_to_snapshot = staticmethod(
        execution_snapshot_service._map_memory_admission_decision_to_snapshot
    )
    list_pending_interactions = execution_interaction_service.list_pending_interactions
    record_interaction_response = execution_interaction_service.record_interaction_response
    apply_task_approval_decision = execution_interaction_service.apply_task_approval_decision
    cancel_task = execution_interaction_service.cancel_task
    get_operational_metrics = execution_snapshot_service.get_operational_metrics
    is_secret_encryption_active = execution_snapshot_service.is_secret_encryption_active
    replay_task = execution_submission_service.replay_task
    _persist_submission = execution_submission_service._persist_submission
    _link_delivery_to_task = execution_submission_service._link_delivery_to_task
    _task_summary = staticmethod(execution_snapshot_service._task_summary)
    _emit_progress = execution_runtime_service._emit_progress
    _run_orchestrator = execution_runtime_service._run_orchestrator
    _load_submission_for_task = execution_submission_service._load_submission_for_task
    _mark_task_in_progress = execution_submission_service._mark_task_in_progress
    _mark_task_failed = execution_submission_service._mark_task_failed
    _release_task_success = execution_submission_service._release_task_success
    _release_task_failure = execution_submission_service._release_task_failure
    _release_task_terminal_failure = execution_submission_service._release_task_terminal_failure
    _record_task_attempt_error = execution_submission_service._record_task_attempt_error
    _heartbeat_task_lease = execution_submission_service._heartbeat_task_lease
    register_worker_node = execution_worker_service.register_worker_node
    ensure_worker_node = execution_worker_service.ensure_worker_node
    heartbeat_worker_node = execution_worker_service.heartbeat_worker_node
    sweep_worker_nodes = execution_worker_service.sweep_worker_nodes
    record_worker_node_success = execution_worker_service.record_worker_node_success
    record_worker_node_failure = execution_worker_service.record_worker_node_failure

    async def _run_blocking(
        self,
        func: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run a synchronous persistence operation in a worker thread, propagating trace context."""

        def _invoke() -> Any:
            return func(*args, **kwargs)

        wrapped = bind_current_trace_context(_invoke, original_func=func)
        return await to_thread.run_sync(wrapped)

    _create_or_get_user = execution_submission_service._create_or_get_user
    _create_or_get_session = execution_submission_service._create_or_get_session
    _persist_execution_outcome = execution_outcome_service._persist_execution_outcome
    _build_friction_proposal_drafts = (
        execution_improvement_proposal_service._build_friction_proposal_drafts
    )
    _score_friction_proposal_drafts = (
        execution_improvement_proposal_service._score_friction_proposal_drafts
    )
    _persist_scored_friction_proposals = (
        execution_improvement_proposal_service._persist_scored_friction_proposals
    )
    _log_task_outcome = execution_snapshot_service._log_task_outcome

    def start_temporal_workflow_sync(self, task_id: str) -> None:
        """Start Temporal workflow from a sync context, spawning background task."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.start_temporal_workflow(task_id))
        except RuntimeError:
            threading.Thread(
                target=lambda: asyncio.run(self.start_temporal_workflow(task_id)), daemon=True
            ).start()

    async def _get_temporal_client(self) -> Any:
        """Get or initialize the shared, pooled Temporal client."""
        current_loop = asyncio.get_running_loop()
        with self._temporal_cache_lock:
            closed_loops = set(self._temporal_clients) | set(self._temporal_locks)
            closed_loops = {loop for loop in closed_loops if loop.is_closed()}
            for closed_loop in closed_loops:
                self._temporal_clients.pop(closed_loop, None)
                self._temporal_locks.pop(closed_loop, None)
            client = self._temporal_clients.get(current_loop)
            loop_lock = self._temporal_locks.get(current_loop)
            if loop_lock is None:
                loop_lock = asyncio.Lock()
                self._temporal_locks[current_loop] = loop_lock

        if client is None:
            async with loop_lock:
                with self._temporal_cache_lock:
                    client = self._temporal_clients.get(current_loop)
                if client is None:
                    from temporalio.client import Client

                    temporal_address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
                    client = await Client.connect(temporal_address)
                    with self._temporal_cache_lock:
                        self._temporal_clients[current_loop] = client
        return client

    def _mark_task_as_failed_on_startup(self, task_id: str, exc: Exception) -> None:
        try:
            from repositories import TaskRepository, TaskTimelineRepository, session_scope

            with session_scope(self.session_factory) as session:
                from db.enums import TimelineEventType

                task_repo = TaskRepository(session)
                timeline_repo = TaskTimelineRepository(session)
                task = task_repo.get(task_id)
                if task and task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    task.status = TaskStatus.FAILED
                    task.last_error = f"Temporal workflow startup failed: {exc}"
                    timeline_repo.create_next_for_attempt(
                        task_id=task_id,
                        attempt_number=task.attempt_count,
                        event_type=TimelineEventType.WORKER_ERROR,
                        message=f"Temporal workflow startup failed: {exc}",
                    )
        except Exception as db_exc:
            logger.error(
                "Failed to transition task %s to FAILED state on startup: %s",
                task_id,
                db_exc,
            )

    async def start_temporal_workflow(self, task_id: str) -> None:
        """Connect to Temporal and start workflow with retries for transient errors."""
        import socket

        import temporalio.exceptions

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                client = await self._get_temporal_client()
                await client.start_workflow(
                    "TaskExecutionWorkflow",
                    task_id,
                    id=f"task-{task_id}",
                    task_queue="task-execution-queue",
                )
                return
            except temporalio.exceptions.WorkflowAlreadyStartedError:
                logger.info("Temporal workflow for task %s is already running.", task_id)
                return
            except (temporalio.service.RPCError, ConnectionError, socket.gaierror) as exc:
                if attempt == max_attempts - 1:
                    logger.exception(
                        "Failed to start Temporal workflow for task %s after %s attempts",
                        task_id,
                        max_attempts,
                    )
                    self._mark_task_as_failed_on_startup(task_id, exc)
                    return
                backoff = 2**attempt
                logger.warning(
                    "Transient error starting workflow for task %s. "
                    "Retrying in %s seconds... Error: %s",
                    task_id,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
            except Exception as exc:
                logger.exception(
                    "Non-retryable exception starting Temporal workflow for task %s",
                    task_id,
                )
                self._mark_task_as_failed_on_startup(task_id, exc)
                return

    async def cancel_temporal_workflow(self, task_id: str) -> None:
        """Connect to Temporal and cancel the workflow."""
        try:
            client = await self._get_temporal_client()
            handle = client.get_workflow_handle(f"task-{task_id}")
            await handle.cancel()
            logger.info(
                "Successfully requested cancellation for Temporal workflow task-%s",
                task_id,
            )
        except Exception as exc:
            logger.exception("Failed to cancel Temporal workflow task-%s: %s", task_id, exc)

    async def signal_temporal_workflow(self, task_id: str, signal_name: str, arg: Any) -> None:
        """Connect to Temporal and send a signal to the workflow, retrying on transient errors."""
        import asyncio

        for attempt in range(3):
            try:
                client = await self._get_temporal_client()
                handle = client.get_workflow_handle(f"task-{task_id}")
                await handle.signal(signal_name, arg)
                return
            except Exception as exc:
                if attempt == 2:
                    logger.exception(
                        "Failed to signal Temporal workflow %s after 3 attempts: %s",
                        task_id,
                        exc,
                    )
                    raise
                logger.warning(
                    "Signal attempt %d failed for task %s, retrying in 1s: %s",
                    attempt + 1,
                    task_id,
                    exc,
                )
                await asyncio.sleep(1)


__all__ = [
    "ApprovalDecisionResult",
    "ArtifactSnapshot",
    "CreateTaskOutcome",
    "DeliveryKey",
    "ExecutionModel",
    "HumanInteractionSnapshot",
    "InteractionInboxCard",
    "InteractionResponse",
    "OperationalMetrics",
    "PersonalMemorySnapshot",
    "PersonalMemoryUpsertRequest",
    "ProgressEvent",
    "ProgressNotifier",
    "ProgressPhase",
    "ProjectMemorySnapshot",
    "ProjectMemoryUpsertRequest",
    "ProposalSnapshot",
    "SessionSnapshot",
    "SessionWorkingContextSnapshot",
    "SubmissionSession",
    "TaskApprovalDecision",
    "TaskClaim",
    "TaskExecutionService",
    "TaskQueueWorker",
    "TaskReplayRequest",
    "TaskReplayResult",
    "TaskSnapshot",
    "TaskSubmission",
    "TaskSubmissionValidationError",
    "TaskSummarySnapshot",
    "TaskTimelineEventSnapshot",
    "WorkerRunSnapshot",
    "_PersistedTaskContext",
    "_apply_execution_budget_policy",
    "_approval_constraints_payload",
    "_artifact_type_for_persistence",
    "_clear_tracing_config_cache",
    "_completion_progress_phase",
    "_deep_merge",
    "_enum_value",
    "_extract_graph_payload",
    "_get_phoenix_url",
    "_get_project_id",
    "_get_trace_id_from_context",
    "_get_tracing_config",
    "_heartbeat_interval_seconds",
    "_interrupt_payload_from_object",
    "_interrupt_summary",
    "_normalize_orchestrator_graph_output",
    "_requires_manual_follow_up",
    "_review_result_artifact_entry",
    "_sanitize_submission_constraints",
    "_serialize_review_result",
    "_serialize_verification_report",
    "_summarize_graph_span_input",
    "_summarize_graph_span_output",
    "_terminal_follow_up_status",
    "_to_json_compatible",
    "_validate_callback_url",
    "_workspace_id_from_artifacts",
    "bootstrap_phoenix_project_id",
    "shutdown_callback_dns_executor",
    "validate_callback_url",
]
