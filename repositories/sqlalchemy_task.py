"""Task-oriented SQLAlchemy repositories."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session, selectinload

from db.enums import (
    HumanInteractionStatus,
    OrchestrationRuntime,
    TaskStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import HumanInteraction, Task, WorkerRun


class TaskRepository:
    """Persist and query tasks."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        session_id: str,
        task_text: str,
        repo_url: str | None = None,
        branch: str | None = None,
        callback_url: str | None = None,
        worker_override: str | WorkerType | None = None,
        constraints: dict[str, Any] | None = None,
        task_spec: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        secrets_encrypted: bool = False,
        status: str = "pending",
        priority: int = 0,
        queue_lane: str = "primary",
        max_attempts: int = 3,
        chosen_worker: str | None = None,
        chosen_profile: str | None = None,
        runtime_mode: str | WorkerRuntimeMode | None = None,
        orchestration_runtime: str | OrchestrationRuntime | None = None,
        route_reason: str | None = None,
        trace_context: dict[str, str] | None = None,
        repair_for_task_id: str | None = None,
    ) -> Task:
        task = Task(
            session_id=session_id,
            task_text=task_text,
            repo_url=repo_url,
            branch=branch,
            callback_url=callback_url,
            worker_override=cast(WorkerType | None, worker_override),
            constraints=constraints or {},
            task_spec=task_spec,
            budget=budget or {},
            secrets=secrets or {},
            secrets_encrypted=secrets_encrypted,
            status=status,
            priority=priority,
            queue_lane=queue_lane,
            max_attempts=max_attempts,
            chosen_worker=chosen_worker,
            chosen_profile=chosen_profile,
            runtime_mode=cast(WorkerRuntimeMode | None, runtime_mode),
            orchestration_runtime=cast(OrchestrationRuntime | None, orchestration_runtime),
            route_reason=route_reason,
            trace_context=trace_context or {},
            repair_for_task_id=repair_for_task_id,
        )
        self.session.add(task)
        self.session.flush()
        return task

    def set_task_spec(self, *, task_id: str, task_spec: dict[str, Any]) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.task_spec = task_spec
        self.session.flush()
        return task

    def get(self, task_id: str) -> Task | None:
        return self.session.get(Task, task_id)

    def list_by_session(self, session_id: str) -> list[Task]:
        statement = (
            select(Task).where(Task.session_id == session_id).order_by(Task.created_at.asc())
        )
        return list(self.session.scalars(statement))

    @staticmethod
    def _latest_run_scalar_subquery(column: Any) -> Any:
        return (
            select(column)
            .where(WorkerRun.task_id == Task.id)
            .order_by(WorkerRun.started_at.desc(), WorkerRun.id.desc())
            .limit(1)
            .scalar_subquery()
        )

    @staticmethod
    def _attach_task_listing_metadata(
        *,
        task: Task,
        latest_run_id: Any,
        latest_run_status: Any,
        latest_run_worker: Any,
        latest_run_requested_permission: Any,
        pending_interaction_count: Any,
    ) -> None:
        setattr(task, "_latest_run_id", latest_run_id)
        setattr(task, "_latest_run_status", latest_run_status)
        setattr(task, "_latest_run_worker", latest_run_worker)
        setattr(task, "_latest_run_requested_permission", latest_run_requested_permission)
        setattr(task, "_pending_interaction_count", int(pending_interaction_count or 0))

    def list_all(
        self,
        *,
        session_id: str | None = None,
        status: str | TaskStatus | None = None,
        limit: int = 50,
        offset: int = 0,
        preload_history: bool = True,
    ) -> list[Task]:
        if preload_history:
            statement = (
                select(Task)
                .options(
                    selectinload(Task.timeline_events),
                    selectinload(Task.worker_runs).selectinload(WorkerRun.artifacts),
                )
                .order_by(Task.created_at.desc())
            )
            if session_id:
                statement = statement.where(Task.session_id == session_id)
            if status:
                status_val = status if isinstance(status, TaskStatus) else TaskStatus(status)
                statement = statement.where(Task.status == status_val)
            statement = statement.limit(max(1, limit)).offset(max(0, offset))
            return list(self.session.scalars(statement))

        latest_run_id_sq = self._latest_run_scalar_subquery(WorkerRun.id)
        latest_run_status_sq = self._latest_run_scalar_subquery(WorkerRun.status)
        latest_run_worker_sq = self._latest_run_scalar_subquery(WorkerRun.worker_type)
        latest_run_requested_permission_sq = self._latest_run_scalar_subquery(
            WorkerRun.requested_permission
        )
        pending_interaction_count_sq = (
            select(func.count(HumanInteraction.id))
            .where(
                HumanInteraction.task_id == Task.id,
                HumanInteraction.status == HumanInteractionStatus.PENDING,
            )
            .scalar_subquery()
        )

        statement = select(
            Task,
            latest_run_id_sq.label("latest_run_id"),
            latest_run_status_sq.label("latest_run_status"),
            latest_run_worker_sq.label("latest_run_worker"),
            latest_run_requested_permission_sq.label("latest_run_requested_permission"),
            pending_interaction_count_sq.label("pending_interaction_count"),
        ).order_by(Task.created_at.desc())

        if session_id:
            statement = statement.where(Task.session_id == session_id)
        if status:
            status_val = status if isinstance(status, TaskStatus) else TaskStatus(status)
            statement = statement.where(Task.status == status_val)

        statement = statement.limit(max(1, limit)).offset(max(0, offset))
        results = self.session.execute(statement).all()

        tasks = []
        for (
            task,
            latest_run_id,
            latest_run_status,
            latest_run_worker,
            latest_run_requested_permission,
            pending_interaction_count,
        ) in results:
            self._attach_task_listing_metadata(
                task=task,
                latest_run_id=latest_run_id,
                latest_run_status=latest_run_status,
                latest_run_worker=latest_run_worker,
                latest_run_requested_permission=latest_run_requested_permission,
                pending_interaction_count=pending_interaction_count,
            )
            tasks.append(task)
        return tasks

    def is_execution_busy(self) -> bool:
        """Return True if any tasks are currently pending or in progress across any queue lane."""
        statement = (
            select(Task.id)
            .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]))
            .limit(1)
        )
        result = self.session.execute(statement).scalar_one_or_none()
        return result is not None

    def set_route(
        self,
        *,
        task_id: str,
        chosen_worker: str | WorkerType,
        chosen_profile: str | None = None,
        runtime_mode: str | WorkerRuntimeMode | None = None,
        route_reason: str,
    ) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.chosen_worker = cast(WorkerType | None, chosen_worker)
        task.chosen_profile = chosen_profile
        task.runtime_mode = cast(WorkerRuntimeMode | None, runtime_mode)
        task.route_reason = route_reason
        self.session.flush()
        return task

    def update_status(self, *, task_id: str, status: str | TaskStatus) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.status = cast(TaskStatus, status)
        self.session.flush()
        return task

    def cancel(self, *, task_id: str) -> tuple[Task | None, bool]:
        task = self.get(task_id)
        if task is None:
            return None, False
        terminal_statuses = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        if task.status in terminal_statuses:
            return task, False
        task.status = TaskStatus.CANCELLED
        task.last_error = "Task cancelled by operator."
        self.session.execute(
            update(HumanInteraction)
            .where(
                HumanInteraction.task_id == task_id,
                HumanInteraction.status == HumanInteractionStatus.PENDING,
            )
            .values(status=HumanInteractionStatus.CANCELLED)
        )
        self.session.flush()
        return task, True

    def get_metrics(self, since: datetime | None = None) -> dict[str, Any]:
        status_stmt = select(Task.status, func.count(Task.id)).group_by(Task.status)
        if since:
            status_stmt = status_stmt.where(Task.created_at >= since)
        status_counts = self.session.execute(status_stmt).all()

        retry_stmt = select(
            func.count(Task.id).label("total"),
            func.coalesce(func.sum(case((Task.attempt_count > 0, 1), else_=0)), 0).label(
                "attempted"
            ),
            func.coalesce(func.sum(case((Task.attempt_count > 1, 1), else_=0)), 0).label("retried"),
        )
        if since:
            retry_stmt = retry_stmt.where(Task.created_at >= since)
        retry_stats = self.session.execute(retry_stmt).one()

        return {
            "status_counts": {
                (s.value if hasattr(s, "value") else str(s)): count for s, count in status_counts
            },
            "total_tasks": retry_stats.total,
            "retried_tasks": retry_stats.retried,
            "retry_rate": (retry_stats.retried / retry_stats.attempted)
            if retry_stats.attempted > 0
            else 0,
        }

    def get_runtime_drain_metrics(self, *, cutover_at: datetime | None = None) -> dict[str, Any]:
        """Return all-time runtime counts used to gate legacy retirement."""

        runtime_counts = self.session.execute(
            select(Task.orchestration_runtime, func.count(Task.id)).group_by(
                Task.orchestration_runtime
            )
        ).all()
        active_legacy_count = self.session.scalar(
            select(func.count(Task.id)).where(
                Task.orchestration_runtime == OrchestrationRuntime.LEGACY,
                Task.status.in_((TaskStatus.PENDING, TaskStatus.IN_PROGRESS)),
            )
        )
        active_unknown_count = self.session.scalar(
            select(func.count(Task.id)).where(
                Task.orchestration_runtime.is_(None),
                Task.status.in_((TaskStatus.PENDING, TaskStatus.IN_PROGRESS)),
            )
        )
        legacy_since_cutover_count = None
        if cutover_at is not None:
            legacy_since_cutover_count = self.session.scalar(
                select(func.count(Task.id)).where(
                    Task.orchestration_runtime == OrchestrationRuntime.LEGACY,
                    Task.created_at >= cutover_at,
                )
            )
        return {
            "orchestration_runtime_counts": {
                (runtime.value if runtime is not None else "unknown"): count
                for runtime, count in runtime_counts
            },
            "active_legacy_task_count": int(active_legacy_count or 0),
            "active_unknown_task_count": int(active_unknown_count or 0),
            "legacy_submissions_since_cutover": (
                int(legacy_since_cutover_count or 0)
                if legacy_since_cutover_count is not None
                else None
            ),
        }
