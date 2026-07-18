"""Runtime execution helpers for task submission and queue processing."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from pydantic import ValidationError

from apps.observability import (
    SPAN_KIND_AGENT,
    record_span_exception,
    set_current_span_attribute,
    set_span_input_output,
    set_span_status_from_outcome,
    start_optional_span,
    with_restored_trace_context,
    with_span_kind,
)
from apps.observability_utils import ATTR_WORKER_ID
from db.base import utc_now
from db.enums import OrchestrationRuntime, TaskStatus
from orchestrator.execution_graph_input import build_orchestrator_graph_input
from orchestrator.execution_policy import (
    _apply_execution_budget_policy,
)
from orchestrator.execution_queue_ownership_service import legacy_worker_may_execute
from orchestrator.execution_serialization import (
    _completion_progress_phase,
    _normalize_orchestrator_graph_output,
    _requires_manual_follow_up,
    _summarize_graph_span_input,
    _summarize_graph_span_output,
    _terminal_follow_up_status,
)
from orchestrator.execution_snapshot_service import _task_summary
from orchestrator.execution_types import (
    ProgressEvent,
    ProgressPhase,
    TaskSubmission,
    _PersistedTaskContext,
)
from orchestrator.execution_worker_service import (
    worker_node_failure_kind_from_exception,
    worker_node_failure_kind_from_state,
)
from orchestrator.state import OrchestratorState
from repositories import TaskTimelineRepository, session_scope

logger = logging.getLogger("orchestrator.execution")


async def _handle_submit_task_error(
    self: Any,
    exc: Exception,
    submission: TaskSubmission,
    persisted: _PersistedTaskContext,
) -> None:
    self._record_execution_span_error(exc)
    logger.exception(
        "Task execution failed before the final outcome was fully persisted",
        extra={
            "session_id": persisted.session_id,
            "task_id": persisted.task_id,
        },
    )
    await self._run_blocking(self._mark_task_failed, task_id=persisted.task_id)
    task_snapshot = await self._run_blocking(self.get_task, persisted.task_id)
    if task_snapshot is None:
        logger.error(
            "Failed to reload task snapshot after marking a background task as failed",
            extra={
                "session_id": persisted.session_id,
                "task_id": persisted.task_id,
            },
        )
        await self._emit_progress(
            submission,
            persisted,
            phase="failed",
            summary="Task execution failed and the final snapshot could not be reloaded.",
        )
        return
    self._log_task_outcome(task_snapshot)
    await self._emit_progress(
        submission,
        persisted,
        phase="failed",
        summary=_task_summary(task_snapshot),
    )


async def submit_task(
    self: Any,
    submission: TaskSubmission,
    persisted: _PersistedTaskContext,
) -> None:
    """Legacy direct execution entrypoint kept for compatibility/tests."""
    if persisted.orchestration_runtime == OrchestrationRuntime.TEMPORAL.value:
        import temporalio.exceptions

        client = await self._get_temporal_client()
        try:
            handle = await client.start_workflow(
                "TaskExecutionWorkflow",
                persisted.task_id,
                id=f"task-{persisted.task_id}",
                task_queue="task-execution-queue",
            )
        except temporalio.exceptions.WorkflowAlreadyStartedError:
            handle = client.get_workflow_handle(f"task-{persisted.task_id}")
        await handle.result()
        return None

    loaded = await self._run_blocking(self._load_submission_for_task, task_id=persisted.task_id)
    if loaded is not None:
        submission = loaded[0]
    span_cm = start_optional_span(
        tracer_name="orchestrator.execution",
        span_name="TaskExecutionService.submit_task",
        attributes=with_span_kind(SPAN_KIND_AGENT),
        task_id=persisted.task_id,
        session_id=persisted.session_id,
        channel=persisted.channel,
    )
    with span_cm:
        set_span_input_output(input_data=submission.task_text)
        await self._run_blocking(self._mark_task_in_progress, task_id=persisted.task_id)
        await self._emit_progress(submission, persisted, phase="started")
        await self._emit_progress(submission, persisted, phase="running")
        started_at = utc_now()
        logger.info(
            "Starting execution-path task run",
            extra={
                "session_id": persisted.session_id,
                "task_id": persisted.task_id,
                "chosen_worker": None,
                "route_reason": None,
                "workspace_id": None,
                "start_timestamp": started_at.isoformat(),
            },
        )

        try:
            state = await self._run_orchestrator(submission, persisted)
            finished_at = utc_now()
            await _persist_execution_outcome_with_reflections(
                self,
                task_id=persisted.task_id,
                state=state,
                started_at=started_at,
                finished_at=finished_at,
            )
            self._update_span_status_from_state(state)
        except Exception as exc:
            await _handle_submit_task_error(self, exc, submission, persisted)
            return None

        await _finalize_task_run(self, submission, persisted)
    return None


async def _finalize_task_run(
    self: Any,
    submission: TaskSubmission,
    persisted: _PersistedTaskContext,
) -> None:
    task_snapshot = await self._run_blocking(self.get_task, persisted.task_id)
    if task_snapshot is None:
        raise RuntimeError(f"Persisted task '{persisted.task_id}' could not be reloaded.")
    self._log_task_outcome(task_snapshot)
    await self._emit_progress(
        submission,
        persisted,
        phase=_completion_progress_phase(task_snapshot),
        summary=_task_summary(task_snapshot),
    )


async def _wait_for_orchestrator_or_heartbeat(
    self: Any,
    task_id: str,
    orchestrator_task: asyncio.Task[OrchestratorState],
    heartbeat_task: asyncio.Task[None],
) -> OrchestratorState | None:
    done, pending = await asyncio.wait(
        [orchestrator_task, heartbeat_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    if heartbeat_task.done():
        heartbeat_exc = heartbeat_task.exception()
        if heartbeat_exc is not None:
            raise heartbeat_exc
        if orchestrator_task in done:
            logger.warning(
                "Orchestrator task completed while heartbeat failed. "
                "Aborting due to heartbeat failure.",
                extra={"task_id": task_id},
            )
            return None
    if orchestrator_task in done:
        return orchestrator_task.result()

    orchestrator_task.cancel()
    try:
        await orchestrator_task
    except asyncio.CancelledError:
        task_snapshot = await self._run_blocking(self.get_task, task_id)
        is_cancelled = task_snapshot and (
            task_snapshot.status == TaskStatus.CANCELLED
            or (
                task_snapshot.status == TaskStatus.FAILED
                and task_snapshot.last_error == "Task cancelled by operator."
            )
        )
        if is_cancelled:
            logger.info(
                "Task execution aborted: task was cancelled",
                extra={"task_id": task_id},
            )
            return None
        logger.warning(
            "Task execution aborted: lease lost or stolen",
            extra={"task_id": task_id},
        )
        return None

    logger.warning(
        "Orchestrator task completed despite cancellation request. "
        "Aborting due to heartbeat failure.",
        extra={"task_id": task_id},
    )
    return None


async def _persist_run_queued_task_outcome(
    self: Any,
    worker_id: str,
    persisted: _PersistedTaskContext,
    state: OrchestratorState,
    started_at: Any,
    finished_at: Any,
) -> None:
    if state.result is not None and state.result.status == "success":
        await _persist_execution_outcome_with_reflections(
            self,
            task_id=persisted.task_id,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
            force_task_status=TaskStatus.COMPLETED,
        )
        await self._run_blocking(
            self._release_task_success, task_id=persisted.task_id, worker_id=worker_id
        )
        await self._run_blocking(self.record_worker_node_success, worker_id=worker_id)
    else:
        failure_kind = worker_node_failure_kind_from_state(state)
        terminal_failure = _requires_manual_follow_up(state)
        terminal_status = _terminal_follow_up_status(
            state=state,
            terminal_failure=terminal_failure,
        )
        await _persist_execution_outcome_with_reflections(
            self,
            task_id=persisted.task_id,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
            force_task_status=terminal_status,
        )
        if terminal_failure:
            await self._run_blocking(
                self._release_task_terminal_failure,
                task_id=persisted.task_id,
                worker_id=worker_id,
                status=terminal_status,
            )
        else:
            await self._run_blocking(
                self._release_task_failure,
                task_id=persisted.task_id,
                worker_id=worker_id,
            )
        await self._run_blocking(
            self.record_worker_node_failure,
            worker_id=worker_id,
            failure_kind=failure_kind,
        )


async def _persist_execution_outcome_with_reflections(
    self: Any,
    *,
    task_id: str,
    state: OrchestratorState,
    started_at: Any,
    finished_at: Any,
    force_task_status: TaskStatus | None = None,
) -> None:
    persisted_outcome = await self._run_blocking(
        self._persist_execution_outcome,
        task_id=task_id,
        state=state,
        started_at=started_at,
        finished_at=finished_at,
        force_task_status=force_task_status,
        persist_friction_proposals=False,
    )
    await _persist_improvement_proposals_for_outcome(
        self,
        state=state,
        persisted_outcome=persisted_outcome,
    )


async def _persist_improvement_proposals_for_outcome(
    self: Any,
    *,
    state: OrchestratorState,
    persisted_outcome: Any,
) -> None:
    try:
        drafts = self._build_friction_proposal_drafts(
            task_id=persisted_outcome.task_id,
            session_id=persisted_outcome.session_id,
            task_constraints=persisted_outcome.task_constraints,
            state=state,
            worker_run_id=persisted_outcome.worker_run_id,
        )
        scored_proposals = await self._score_friction_proposal_drafts(drafts=drafts)
        if not scored_proposals:
            return
        await self._run_blocking(
            self._persist_scored_friction_proposals,
            scored_proposals=scored_proposals,
        )
    except Exception as exc:
        logger.warning("Failed to persist friction proposals: %s", exc, exc_info=True)


async def _handle_run_queued_task_error(
    self: Any,
    exc: Exception,
    worker_id: str,
    persisted: _PersistedTaskContext,
) -> None:
    self._record_execution_span_error(exc)
    logger.exception(
        "Task execution failed before the final outcome was fully persisted",
        extra={
            "session_id": persisted.session_id,
            "task_id": persisted.task_id,
            "worker_id": worker_id,
        },
    )
    await self._run_blocking(
        self._record_task_attempt_error,
        task_id=persisted.task_id,
        error=f"{type(exc).__name__}: {exc}",
    )
    await self._run_blocking(
        self._release_task_failure,
        task_id=persisted.task_id,
        worker_id=worker_id,
    )
    failure_kind = worker_node_failure_kind_from_exception(exc)
    await self._run_blocking(
        self.record_worker_node_failure,
        worker_id=worker_id,
        failure_kind=failure_kind,
    )


async def _handle_invalid_queued_submission(
    self: Any,
    exc: ValidationError,
    worker_id: str,
    task_id: str,
) -> None:
    """Fail queued tasks whose persisted submission cannot be validated."""
    self._record_execution_span_error(exc)
    logger.exception(
        "Queued task submission failed validation before execution",
        extra={
            "task_id": task_id,
            "worker_id": worker_id,
        },
    )
    await self._run_blocking(
        self._record_task_attempt_error,
        task_id=task_id,
        error=f"{type(exc).__name__}: {exc}",
    )
    await self._run_blocking(
        self._release_task_terminal_failure,
        task_id=task_id,
        worker_id=worker_id,
    )


async def run_queued_task(
    self: Any,
    *,
    task_id: str,
    worker_id: str,
    lease_seconds: int = 60,
) -> None:
    """Execute one claimed queued task id and persist/release queue state."""
    try:
        loaded = await self._run_blocking(self._load_submission_for_task, task_id=task_id)
    except ValidationError as exc:
        return await _handle_invalid_queued_submission(self, exc, worker_id, task_id)
    if loaded is None:
        logger.warning("Skipping queued task run: task no longer exists; id=%s", task_id)
        return None

    submission, persisted = loaded
    if not await legacy_worker_may_execute(
        self,
        task_id=task_id,
        worker_id=worker_id,
        orchestration_runtime=persisted.orchestration_runtime,
    ):
        return None
    execution_facade = sys.modules.get("orchestrator.execution")
    restored_trace_context = getattr(
        execution_facade,
        "with_restored_trace_context",
        with_restored_trace_context,
    )
    with restored_trace_context(persisted.trace_context):
        span_cm = start_optional_span(
            tracer_name="orchestrator.execution",
            span_name="TaskExecutionService.run_queued_task",
            attributes=with_span_kind(SPAN_KIND_AGENT),
            task_id=persisted.task_id,
            session_id=persisted.session_id,
            channel=persisted.channel,
        )
        with span_cm:
            set_current_span_attribute(ATTR_WORKER_ID, worker_id)
            set_span_input_output(input_data=submission.task_text)
            await self._emit_progress(submission, persisted, phase="started")
            await self._emit_progress(submission, persisted, phase="running")

            started_at = utc_now()
            try:
                orchestrator_task = asyncio.create_task(
                    self._run_orchestrator(submission, persisted),
                    name=f"orchestrator-{task_id}",
                )
                heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(
                        task_id=task_id,
                        worker_id=worker_id,
                        lease_seconds=lease_seconds,
                    ),
                    name=f"task-heartbeat-{task_id}",
                )

                state = await _wait_for_orchestrator_or_heartbeat(
                    self, task_id, orchestrator_task, heartbeat_task
                )
                if state is None:
                    return None

                finished_at = utc_now()
                self._update_span_status_from_state(state)

                await _persist_run_queued_task_outcome(
                    self, worker_id, persisted, state, started_at, finished_at
                )
            except Exception as exc:
                await _handle_run_queued_task_error(self, exc, worker_id, persisted)
            finally:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

            await _finalize_task_run(self, submission, persisted)
    return None


def _update_span_status_from_state(self: Any, state: OrchestratorState) -> None:
    """Update the current span status based on the orchestrator state outcomes."""
    if "blocked_on_clarification" in state.errors:
        set_span_status_from_outcome("blocked_on_clarification", "awaiting clarification")
    elif state.errors:
        set_span_status_from_outcome("error", state.errors[0])
    elif state.result is not None:
        set_span_status_from_outcome(state.result.status, state.result.summary)


def _record_execution_span_error(self: Any, exc: Exception) -> None:
    """Log and record a span error for a task execution failure."""
    logger.debug("Task execution failed: %s", exc, exc_info=True)
    record_span_exception(exc)
    set_span_status_from_outcome("error", str(exc))


async def _emit_progress(
    self: Any,
    submission: TaskSubmission,
    persisted: _PersistedTaskContext,
    *,
    phase: ProgressPhase,
    summary: str | None = None,
) -> None:
    """Best-effort lifecycle notification that never breaks task execution."""
    if self.progress_notifier is None:
        return
    event = ProgressEvent(
        phase=phase,
        task_id=persisted.task_id,
        session_id=persisted.session_id,
        channel=persisted.channel,
        external_thread_id=persisted.external_thread_id,
        task_text=submission.task_text,
        summary=summary,
    )
    try:
        await self.progress_notifier.notify(submission=submission, event=event)
    except Exception:
        logger.warning(
            "Progress notification failed",
            exc_info=True,
            extra={
                "session_id": persisted.session_id,
                "task_id": persisted.task_id,
                "phase": phase,
            },
        )


async def _run_orchestrator(
    self: Any,
    submission: TaskSubmission,
    persisted: _PersistedTaskContext,
) -> OrchestratorState:
    """Execute the orchestrator graph for one submitted task."""

    def _get_count() -> int:
        with session_scope(self.session_factory) as session:
            return TaskTimelineRepository(session).count_by_attempt(
                task_id=persisted.task_id,
                attempt_number=persisted.attempt_count,
            )

    initial_persisted_count = await self._run_blocking(_get_count)
    config = {"configurable": {"thread_id": persisted.task_id}}
    effective_budget = _apply_execution_budget_policy(
        channel=persisted.channel,
        constraints=submission.constraints,
        budget=submission.budget,
    )

    span_cm = start_optional_span(
        tracer_name="orchestrator.execution",
        span_name=(
            f"orchestrator.graph.run (Attempt {persisted.attempt_count})"
            if persisted.attempt_count > 1
            else "orchestrator.graph.run"
        ),
        attributes=with_span_kind(SPAN_KIND_AGENT),
        task_id=persisted.task_id,
        session_id=persisted.session_id,
        attempt=persisted.attempt_count,
        channel=persisted.channel,
    )
    with span_cm:
        graph_input = build_orchestrator_graph_input(
            submission,
            persisted,
            effective_budget,
            initial_persisted_count,
        )

        set_span_input_output(input_data=_summarize_graph_span_input(graph_input))
        raw_output = await self.graph.ainvoke(graph_input, config=config)
        set_span_input_output(
            input_data=None,
            output_data=_summarize_graph_span_output(raw_output),
        )

    normalized_output = _normalize_orchestrator_graph_output(raw_output)
    return OrchestratorState.model_validate(normalized_output)
