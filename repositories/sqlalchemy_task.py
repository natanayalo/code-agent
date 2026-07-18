"""Task-oriented SQLAlchemy repositories."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from db.enums import (
    HumanInteractionStatus,
    OrchestrationRuntime,
    TaskStatus,
    WorkerNodeStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import HumanInteraction, Task, WorkerNode, WorkerRun
from repositories.sqlalchemy_worker import WorkerNodeRepository


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
        next_attempt_at: datetime | None = None,
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
            next_attempt_at=next_attempt_at,
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

    @staticmethod
    def _claimable_pending_filter(*, now: datetime) -> Any:
        return and_(
            Task.status == TaskStatus.PENDING,
            or_(
                and_(Task.attempt_count == 0, Task.next_attempt_at.is_(None)),
                Task.next_attempt_at <= now,
            ),
        )

    @staticmethod
    def _normalize_profile_name(profile: str) -> str:
        stripped = profile.strip()
        if stripped.lower().startswith("gemini-"):
            return "antigravity-" + stripped[7:]
        return stripped

    @staticmethod
    def _task_profile_override(task: Task) -> str | None:
        constraints = task.constraints if isinstance(task.constraints, dict) else {}
        raw_profile = constraints.get("worker_profile_override")
        if isinstance(raw_profile, str) and raw_profile.strip():
            return TaskRepository._normalize_profile_name(raw_profile)
        return None

    @staticmethod
    def _task_matches_worker_node(task: Task, worker_node: WorkerNode) -> bool:
        supported_worker_types = WorkerNodeRepository.supported_worker_types(worker_node)
        required_worker_types = {
            worker_type
            for worker_type in (task.worker_override, task.chosen_worker)
            if worker_type is not None
        }
        if required_worker_types and not required_worker_types <= supported_worker_types:
            return False

        supported_profiles = {
            TaskRepository._normalize_profile_name(profile)
            for profile in (worker_node.supported_profiles or [])
            if isinstance(profile, str) and profile.strip()
        }
        required_profiles = {
            profile
            for profile in (task.chosen_profile, TaskRepository._task_profile_override(task))
            if profile
        }
        required_profiles = {
            TaskRepository._normalize_profile_name(profile) for profile in required_profiles
        }
        if required_profiles and not required_profiles <= supported_profiles:
            return False

        capabilities = (
            worker_node.capabilities if isinstance(worker_node.capabilities, dict) else {}
        )
        supported_lanes = capabilities.get("lanes")
        if isinstance(supported_lanes, list):
            lane_values = {lane for lane in supported_lanes if isinstance(lane, str)}
            if lane_values and task.queue_lane not in lane_values:
                return False
        return True

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

    @staticmethod
    def _ensure_worker_node_for_claim(
        *,
        worker_repo: WorkerNodeRepository,
        worker_id: str,
        now: datetime,
    ) -> WorkerNode:
        worker_node = worker_repo.get_by_worker_id(worker_id)
        if worker_node is not None:
            return worker_node
        return worker_repo.register_worker(
            worker_id=worker_id,
            worker_type=WorkerType.CODEX,
            now=now,
            capacity=1,
            capabilities={
                "worker_types": [worker_type.value for worker_type in WorkerType],
                "lanes": ["primary", "scout"],
            },
        )

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> Task | None:
        # Expired leases are reclaimed asynchronously by the background sweep service
        worker_repo = WorkerNodeRepository(self.session)
        worker_node = self._ensure_worker_node_for_claim(
            worker_repo=worker_repo,
            worker_id=worker_id,
            now=now,
        )
        if (
            worker_node is None
            or worker_node.status != WorkerNodeStatus.ACTIVE
            or worker_node.current_load >= worker_node.capacity
        ):
            return None
        limit = 25
        offset = 0
        lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))

        while True:
            candidates = list(
                self.session.scalars(
                    select(Task)
                    .where(
                        self._claimable_pending_filter(now=now),
                        Task.orchestration_runtime == OrchestrationRuntime.LEGACY,
                    )
                    .order_by(
                        case((Task.queue_lane == "primary", 1), else_=2).asc(),
                        Task.priority.desc(),
                        Task.created_at.asc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            )
            if not candidates:
                return None

            matching_candidates = [
                candidate
                for candidate in candidates
                if self._task_matches_worker_node(candidate, worker_node)
            ]

            if not matching_candidates:
                offset += limit
                continue

            for candidate in matching_candidates:
                claimed = self.session.execute(
                    update(Task)
                    .where(
                        Task.id == candidate.id,
                        self._claimable_pending_filter(now=now),
                        Task.orchestration_runtime == OrchestrationRuntime.LEGACY,
                    )
                    .values(
                        status=TaskStatus.IN_PROGRESS,
                        lease_owner=worker_id,
                        lease_expires_at=lease_expires_at,
                        attempt_count=Task.attempt_count + 1,
                        last_error=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                claimed_rows = int(getattr(claimed, "rowcount", 0) or 0)
                if claimed_rows > 0:
                    if not worker_repo.reserve_load(worker_id=worker_id):
                        # Revert the claim if capacity was exceeded concurrently
                        self.session.execute(
                            update(Task)
                            .where(
                                Task.id == candidate.id,
                                Task.status == TaskStatus.IN_PROGRESS,
                                Task.lease_owner == worker_id,
                            )
                            .values(
                                status=TaskStatus.PENDING,
                                lease_owner=None,
                                lease_expires_at=None,
                                attempt_count=candidate.attempt_count,
                                last_error=candidate.last_error,
                            )
                            .execution_options(synchronize_session=False)
                        )
                        self.session.flush()
                        return None
                    self.session.flush()
                    return self.session.execute(
                        select(Task)
                        .where(Task.id == candidate.id)
                        .execution_options(populate_existing=True)
                    ).scalar_one_or_none()

            # If we couldn't claim any of the matching candidates (e.g. concurrent claims),
            # we can safely just return None and retry next poll, rather than
            # risking skipping rows with an offset on a mutating dataset.
            return None

    def reclaim_expired_leases(self, *, now: datetime) -> int:
        """Return expired leases to pending and rebuild affected worker load."""
        expired_tasks_info = self.session.execute(
            select(Task.id, Task.lease_owner).where(
                Task.orchestration_runtime == OrchestrationRuntime.LEGACY,
                Task.status == TaskStatus.IN_PROGRESS,
                Task.lease_expires_at.is_not(None),
                Task.lease_expires_at <= now,
            )
        ).all()

        if not expired_tasks_info:
            return 0

        task_ids = [row.id for row in expired_tasks_info]
        affected_workers = sorted(
            list({row.lease_owner for row in expired_tasks_info if row.lease_owner})
        )

        # 1. Lock and update Tasks first to maintain consistent locking order (Task -> WorkerNode)
        # and prevent deadlocks with release_success/release_failure.
        updated = self.session.execute(
            update(Task)
            .where(
                Task.id.in_(task_ids),
                Task.orchestration_runtime == OrchestrationRuntime.LEGACY,
                Task.status == TaskStatus.IN_PROGRESS,
            )
            .values(
                status=case(
                    (Task.attempt_count >= Task.max_attempts, TaskStatus.FAILED),
                    else_=TaskStatus.PENDING,
                ),
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=case(
                    (Task.attempt_count >= Task.max_attempts, None),
                    else_=now,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        updated_count = getattr(updated, "rowcount", 0) or 0
        if updated_count <= 0:
            return 0
        self.session.flush()

        # 2. Update the WorkerNode load for affected workers
        worker_repo = WorkerNodeRepository(self.session)
        if affected_workers:
            # Lock all affected WorkerNodes in a single sorted query to prevent deadlocks
            self.session.scalars(
                select(WorkerNode.id)
                .where(WorkerNode.worker_id.in_(affected_workers))
                .order_by(WorkerNode.worker_id.asc())
                .with_for_update()
            ).all()

            # Query remaining loads for all affected workers in a single grouped query
            loads_query = (
                select(Task.lease_owner, func.count(Task.id))
                .where(
                    Task.orchestration_runtime == OrchestrationRuntime.LEGACY,
                    Task.lease_owner.in_(affected_workers),
                    Task.status == TaskStatus.IN_PROGRESS,
                )
                .group_by(Task.lease_owner)
            )
            remaining_loads = {row[0]: row[1] for row in self.session.execute(loads_query).all()}

            for worker_id in affected_workers:
                remaining_load = remaining_loads.get(worker_id, 0)
                worker_repo.set_load(worker_id=worker_id, current_load=remaining_load)

        return updated_count

    def heartbeat_lease(
        self,
        *,
        task_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
        updated = self.session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.orchestration_runtime == OrchestrationRuntime.LEGACY,
                Task.status == TaskStatus.IN_PROGRESS,
                Task.lease_owner == worker_id,
            )
            .values(lease_expires_at=lease_expires_at)
            .execution_options(synchronize_session=False)
        )
        updated_rows = int(getattr(updated, "rowcount", 0) or 0)
        if updated_rows > 0:
            self.session.flush()
            return True
        return False

    def release_success(self, *, task_id: str, worker_id: str) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        if (
            task.orchestration_runtime != OrchestrationRuntime.LEGACY
            or task.lease_owner != worker_id
        ):
            return task
        task.status = TaskStatus.COMPLETED
        task.lease_owner = None
        task.lease_expires_at = None
        task.next_attempt_at = None
        task.last_error = None
        self.session.flush()
        WorkerNodeRepository(self.session).release_load(worker_id=worker_id)
        return task

    def release_failure(
        self,
        *,
        task_id: str,
        worker_id: str,
        now: datetime,
        retry_backoff_seconds: int,
    ) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        if (
            task.orchestration_runtime != OrchestrationRuntime.LEGACY
            or task.status != TaskStatus.IN_PROGRESS
            or task.lease_owner != worker_id
        ):
            return task

        previous_owner = task.lease_owner
        task.lease_owner = None
        task.lease_expires_at = None
        if task.attempt_count >= task.max_attempts:
            task.status = TaskStatus.FAILED
            task.next_attempt_at = None
        else:
            task.status = TaskStatus.PENDING
            task.next_attempt_at = now + timedelta(seconds=max(0, retry_backoff_seconds))
        self.session.flush()
        if previous_owner:
            WorkerNodeRepository(self.session).release_load(worker_id=previous_owner)
        return task

    def release_terminal_failure(
        self,
        *,
        task_id: str,
        worker_id: str,
        status: TaskStatus = TaskStatus.FAILED,
    ) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        if (
            task.orchestration_runtime != OrchestrationRuntime.LEGACY
            or task.lease_owner != worker_id
        ):
            return task
        previous_owner = task.lease_owner
        task.lease_owner = None
        task.lease_expires_at = None
        task.status = status
        task.next_attempt_at = None
        self.session.flush()
        if previous_owner:
            WorkerNodeRepository(self.session).release_load(worker_id=previous_owner)
        return task

    def cancel(self, *, task_id: str) -> tuple[Task | None, bool]:
        task = self.get(task_id)
        if task is None:
            return None, False
        terminal_statuses = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        if task.status in terminal_statuses:
            return task, False
        previous_owner = task.lease_owner
        task.lease_owner = None
        task.lease_expires_at = None
        task.next_attempt_at = None
        task.status = TaskStatus.FAILED
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
        if previous_owner:
            WorkerNodeRepository(self.session).release_load(worker_id=previous_owner)
        return task, True

    def record_attempt_error(
        self,
        *,
        task_id: str,
        error_text: str,
    ) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.last_error = error_text[:4000]
        self.session.flush()
        return task

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

    def get_runtime_drain_metrics(self) -> dict[str, Any]:
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
        return {
            "orchestration_runtime_counts": {
                (runtime.value if runtime is not None else "unknown"): count
                for runtime, count in runtime_counts
            },
            "active_legacy_task_count": int(active_legacy_count or 0),
            "active_unknown_task_count": int(active_unknown_count or 0),
        }

    def release_runtime_ownership_violation(self, *, task_id: str, worker_id: str) -> bool:
        """Release a non-legacy task accidentally handed to the legacy worker."""

        released = self.session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == TaskStatus.IN_PROGRESS,
                Task.lease_owner == worker_id,
                or_(
                    Task.orchestration_runtime.is_(None),
                    Task.orchestration_runtime != OrchestrationRuntime.LEGACY,
                ),
            )
            .values(
                status=TaskStatus.PENDING,
                lease_owner=None,
                lease_expires_at=None,
                attempt_count=case(
                    (Task.attempt_count > 0, Task.attempt_count - 1),
                    else_=0,
                ),
                last_error="Legacy worker refused task due to orchestration runtime ownership.",
            )
        )
        self.session.flush()
        return bool(getattr(released, "rowcount", 0))
