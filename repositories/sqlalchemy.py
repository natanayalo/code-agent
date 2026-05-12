"""SQLAlchemy-backed repositories for persistence entities."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final, cast
from uuid import uuid4

from sqlalchemy import and_, case, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from db.base import utc_now
from db.enums import (
    ArtifactType,
    HumanInteractionStatus,
    HumanInteractionType,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import (
    Artifact,
    HumanInteraction,
    InboundDelivery,
    PersonalMemory,
    ProjectMemory,
    SessionState,
    Task,
    TaskTimelineEvent,
    User,
    WorkerRun,
)
from db.models import (
    Session as ConversationSession,
)

_UNSET: Final = object()


class UserRepository:
    """Persist and query users."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        external_user_id: str | None = None,
        display_name: str | None = None,
    ) -> User:
        user = User(external_user_id=external_user_id, display_name=display_name)
        self.session.add(user)
        self.session.flush()
        return user

    def get(self, user_id: str) -> User | None:
        return self.session.get(User, user_id)

    def get_by_external_user_id(self, external_user_id: str) -> User | None:
        statement = select(User).where(User.external_user_id == external_user_id)
        return self.session.scalar(statement)


def _apply_memory_metadata(
    memory_entry: PersonalMemory | ProjectMemory,
    *,
    value: dict[str, Any],
    source: str | None | object = _UNSET,
    confidence: float | object = _UNSET,
    scope: str | None | object = _UNSET,
    last_verified_at: datetime | None | object = _UNSET,
    requires_verification: bool | object = _UNSET,
) -> None:
    """Apply the shared skeptical-memory metadata fields to a memory entry."""

    memory_entry.value = value
    if source is not _UNSET:
        memory_entry.source = cast(str | None, source)
    if confidence is not _UNSET:
        memory_entry.confidence = cast(float, confidence)
    if scope is not _UNSET:
        memory_entry.scope = cast(str | None, scope)
    if last_verified_at is not _UNSET:
        memory_entry.last_verified_at = cast(datetime | None, last_verified_at)
    if requires_verification is not _UNSET:
        memory_entry.requires_verification = cast(bool, requires_verification)


class SessionRepository:
    """Persist and query conversation sessions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        user_id: str,
        channel: str,
        external_thread_id: str,
        active_task_id: str | None = None,
        status: str = "active",
        last_seen_at: datetime | None = None,
    ) -> ConversationSession:
        conversation_session = ConversationSession(
            user_id=user_id,
            channel=channel,
            external_thread_id=external_thread_id,
            active_task_id=active_task_id,
            status=status,
            last_seen_at=last_seen_at,
        )
        self.session.add(conversation_session)
        self.session.flush()
        return conversation_session

    def get(self, session_id: str) -> ConversationSession | None:
        statement = (
            select(ConversationSession)
            .options(selectinload(ConversationSession.session_state))
            .where(ConversationSession.id == session_id)
        )
        return self.session.scalar(statement)

    def get_by_channel_thread(
        self,
        *,
        channel: str,
        external_thread_id: str,
    ) -> ConversationSession | None:
        statement = select(ConversationSession).where(
            ConversationSession.channel == channel,
            ConversationSession.external_thread_id == external_thread_id,
        )
        return self.session.scalar(statement)

    def list_by_user(self, user_id: str) -> list[ConversationSession]:
        statement = (
            select(ConversationSession)
            .where(ConversationSession.user_id == user_id)
            .order_by(ConversationSession.created_at.asc())
        )
        return list(self.session.scalars(statement))

    def list_all(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationSession]:
        """List all sessions with pagination."""
        statement = (
            select(ConversationSession)
            .options(selectinload(ConversationSession.session_state))
            .order_by(ConversationSession.created_at.desc())
            .limit(max(1, limit))
            .offset(max(0, offset))
        )
        return list(self.session.scalars(statement))

    def set_active_task(
        self,
        *,
        session_id: str,
        active_task_id: str | None,
    ) -> ConversationSession | None:
        conversation_session = self.get(session_id)
        if conversation_session is None:
            return None

        conversation_session.active_task_id = active_task_id
        self.session.flush()
        return conversation_session

    def touch(
        self,
        *,
        session_id: str,
        seen_at: datetime,
    ) -> ConversationSession | None:
        conversation_session = self.get(session_id)
        if conversation_session is None:
            return None

        conversation_session.last_seen_at = seen_at
        self.session.flush()
        return conversation_session


class SessionStateRepository:
    """Persist and query compact session working state."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, session_id: str) -> SessionState | None:
        statement = select(SessionState).where(SessionState.session_id == session_id)
        return self.session.scalar(statement)

    def upsert(
        self,
        *,
        session_id: str,
        active_goal: str | None = None,
        decisions_made: dict[str, Any] | None = None,
        identified_risks: dict[str, Any] | None = None,
        files_touched: list[str] | None = None,
    ) -> SessionState:
        state = self.get(session_id)
        if state is None:
            state = SessionState(
                session_id=session_id,
                active_goal=active_goal,
                decisions_made=decisions_made or {},
                identified_risks=identified_risks or {},
                files_touched=files_touched or [],
            )
            try:
                with self.session.begin_nested():
                    self.session.add(state)
                    self.session.flush()
                return state
            except IntegrityError:
                state = self.get(session_id)
                if state is None:
                    raise

        # Update either the existing or concurrently-inserted state
        if active_goal is not None:
            state.active_goal = active_goal
        if decisions_made is not None:
            state.decisions_made = {**(state.decisions_made or {}), **decisions_made}
        if identified_risks is not None:
            state.identified_risks = {**(state.identified_risks or {}), **identified_risks}
        if files_touched is not None:
            state.files_touched = list(
                dict.fromkeys([*(state.files_touched or []), *files_touched])
            )
        self.session.flush()

        return state


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
        max_attempts: int = 3,
        next_attempt_at: datetime | None = None,
        chosen_worker: str | None = None,
        chosen_profile: str | None = None,
        runtime_mode: str | WorkerRuntimeMode | None = None,
        route_reason: str | None = None,
        trace_context: dict[str, str] | None = None,
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
            max_attempts=max_attempts,
            next_attempt_at=next_attempt_at,
            chosen_worker=chosen_worker,
            chosen_profile=chosen_profile,
            runtime_mode=cast(WorkerRuntimeMode | None, runtime_mode),
            route_reason=route_reason,
            trace_context=trace_context or {},
        )
        self.session.add(task)
        self.session.flush()
        return task

    def set_task_spec(self, *, task_id: str, task_spec: dict[str, Any]) -> Task | None:
        """Persist the generated structured task contract for a task."""
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
        # Attach temporary attributes used by TaskExecutionService summary mapping.
        setattr(task, "_latest_run_id", latest_run_id)
        setattr(task, "_latest_run_status", latest_run_status)
        setattr(task, "_latest_run_worker", latest_run_worker)
        setattr(task, "_latest_run_requested_permission", latest_run_requested_permission)
        setattr(task, "_pending_interaction_count", int(pending_interaction_count or 0))

    @staticmethod
    def _claimable_pending_filter(*, now: datetime) -> Any:
        """Return the pending-task predicate used by both select and claim CAS update."""
        return and_(
            Task.status == TaskStatus.PENDING,
            or_(
                and_(Task.attempt_count == 0, Task.next_attempt_at.is_(None)),
                Task.next_attempt_at <= now,
            ),
        )

    def list_all(
        self,
        *,
        session_id: str | None = None,
        status: str | TaskStatus | None = None,
        limit: int = 50,
        offset: int = 0,
        preload_history: bool = True,
    ) -> list[Task]:
        """List all tasks with optional filtering and pagination.

        Uses selectinload to eagerly load related runs, artifacts, and timeline events (T-131).
        If preload_history is False, only the basic Task object is fetched, with latest
        run metadata joined via scalar subqueries (highly optimized for listing).
        """
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
        else:
            # Optimized listing path: join latest run metadata via scalar subqueries (T-131)
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

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> Task | None:
        """Claim one available task for execution.

        Claim order is highest priority then oldest created task. Claiming is optimistic/CAS:
        if another worker has already claimed the selected task, this call retries with the next
        candidate in the ordered set.
        """
        lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
        candidates = list(
            self.session.scalars(
                select(Task.id)
                .where(
                    or_(
                        self._claimable_pending_filter(now=now),
                        and_(
                            Task.status == TaskStatus.IN_PROGRESS,
                            Task.lease_expires_at.is_not(None),
                            Task.lease_expires_at <= now,
                        ),
                    )
                )
                .order_by(Task.priority.desc(), Task.created_at.asc())
                .limit(25)
            )
        )
        for task_id in candidates:
            claimed = self.session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    or_(
                        self._claimable_pending_filter(now=now),
                        and_(
                            Task.status == TaskStatus.IN_PROGRESS,
                            Task.lease_expires_at.is_not(None),
                            Task.lease_expires_at <= now,
                        ),
                    ),
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
                self.session.flush()
                return self.session.execute(
                    select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
                ).scalar_one_or_none()
        return None

    def heartbeat_lease(
        self,
        *,
        task_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        """Extend a claimed task lease when ownership still matches."""
        lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
        updated = self.session.execute(
            update(Task)
            .where(
                Task.id == task_id,
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

    def release_success(
        self,
        *,
        task_id: str,
    ) -> Task | None:
        """Mark task successful and clear queue lease state."""
        task = self.get(task_id)
        if task is None:
            return None
        task.status = TaskStatus.COMPLETED
        task.lease_owner = None
        task.lease_expires_at = None
        task.next_attempt_at = None
        task.last_error = None
        self.session.flush()
        return task

    def release_failure(
        self,
        *,
        task_id: str,
        worker_id: str,
        now: datetime,
        retry_backoff_seconds: int,
    ) -> Task | None:
        """Release a failed attempt, either requeueing or marking terminal failure."""
        task = self.get(task_id)
        if task is None:
            return None
        if task.status != TaskStatus.IN_PROGRESS or task.lease_owner != worker_id:
            return task

        task.lease_owner = None
        task.lease_expires_at = None
        if task.attempt_count >= task.max_attempts:
            task.status = TaskStatus.FAILED
            task.next_attempt_at = None
        else:
            task.status = TaskStatus.PENDING
            task.next_attempt_at = now + timedelta(seconds=max(0, retry_backoff_seconds))
        self.session.flush()
        return task

    def release_terminal_failure(
        self,
        *,
        task_id: str,
        worker_id: str,
        status: TaskStatus = TaskStatus.FAILED,
    ) -> Task | None:
        """Mark a claimed task as terminally failed/paused and clear queue lease state."""
        task = self.get(task_id)
        if task is None:
            return None
        if task.lease_owner == worker_id:
            task.lease_owner = None
            task.lease_expires_at = None
        task.status = status
        task.next_attempt_at = None
        self.session.flush()
        return task

    def cancel(self, *, task_id: str) -> tuple[Task | None, bool]:
        """Mark a task as terminally cancelled and clear queue lease state.

        Returns (task, was_cancelled).
        """
        task = self.get(task_id)
        if task is None:
            return None, False

        # Enforce that only non-terminal tasks can be transitioned
        terminal_statuses = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        if task.status in terminal_statuses:
            return task, False

        # Terminate any active lease
        task.lease_owner = None
        task.lease_expires_at = None
        task.next_attempt_at = None
        # Resulting state aligns with requirement that operator-initiated rejections
        # result in FAILED
        task.status = TaskStatus.FAILED
        task.last_error = "Task cancelled by operator."

        # Cancel any pending interactions to prevent operator confusion
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

    def record_attempt_error(
        self,
        *,
        task_id: str,
        error_text: str,
    ) -> Task | None:
        """Persist a bounded textual error for observability/debugging."""
        task = self.get(task_id)
        if task is None:
            return None
        task.last_error = error_text[:4000]
        self.session.flush()
        return task

    def get_metrics(self, since: datetime | None = None) -> dict[str, Any]:
        """Aggregate high-level task status and retry metrics."""
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


class HumanInteractionRepository:
    """Persist and query human interaction checkpoints."""

    _TASK_SPEC_SOURCE = "task_spec"
    _TASK_SPEC_INTERACTION_TYPES = (
        HumanInteractionType.CLARIFICATION,
        HumanInteractionType.PERMISSION,
    )

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_by_task(
        self,
        *,
        task_id: str,
        interaction_types: tuple[HumanInteractionType, ...] | None = None,
        statuses: tuple[HumanInteractionStatus, ...] | None = None,
    ) -> list[HumanInteraction]:
        statement = (
            select(HumanInteraction)
            .where(HumanInteraction.task_id == task_id)
            .order_by(HumanInteraction.created_at.asc())
        )
        if interaction_types is not None:
            statement = statement.where(HumanInteraction.interaction_type.in_(interaction_types))
        if statuses is not None:
            statement = statement.where(HumanInteraction.status.in_(statuses))
        return list(self.session.scalars(statement))

    def record_response(
        self,
        interaction_id: str,
        *,
        task_id: str,
        response_data: Mapping[str, Any],
        status: HumanInteractionStatus = HumanInteractionStatus.RESOLVED,
    ) -> tuple[HumanInteraction | None, bool]:
        """Apply an idempotent response to a pending human interaction.

        Returns (interaction, applied).
        - If interaction is not found, returns (None, False).
        - If interaction exists but task_id mismatches, returns (None, False).
        - If interaction is already terminal, returns (interaction, False) because
          no new state transition is persisted.
        """
        interaction = self.session.get(HumanInteraction, interaction_id)
        if interaction is None or interaction.task_id != task_id:
            return None, False

        # Idempotency check: if already terminal
        if interaction.status != HumanInteractionStatus.PENDING:
            return interaction, False

        interaction.status = status
        interaction.response_data = dict(response_data)
        interaction.updated_at = utc_now()
        self.session.flush()
        return interaction, True

    def sync_task_spec_flags(
        self, *, task_id: str, task_spec: dict[str, Any]
    ) -> list[HumanInteraction]:
        """Map TaskSpec clarification/permission flags into pending interaction rows."""
        desired: dict[HumanInteractionType, tuple[str, dict[str, Any]]] = {}

        if bool(task_spec.get("requires_clarification")):
            raw_questions = task_spec.get("clarification_questions")
            clarification_questions = raw_questions if isinstance(raw_questions, list) else []
            goal_text_raw = task_spec.get("goal")
            goal_text = goal_text_raw.strip() if isinstance(goal_text_raw, str) else ""
            questions = [
                question.strip()
                for question in clarification_questions
                if isinstance(question, str) and question.strip()
            ]
            if not questions:
                if goal_text:
                    questions = [
                        "What exact repo, files, behavior, or failure should the worker target "
                        f"for: {goal_text}?"
                    ]
                else:
                    questions = [
                        "What exact repo, files, behavior, or failure should the worker target?"
                    ]
            desired[HumanInteractionType.CLARIFICATION] = (
                "Task requires clarification before execution can continue.",
                {
                    "source": self._TASK_SPEC_SOURCE,
                    "resume_token": f"clarification-{task_id}",
                    "questions": questions,
                },
            )

        if bool(task_spec.get("requires_permission")):
            reason_raw = task_spec.get("permission_reason")
            reason = (
                reason_raw.strip()
                if isinstance(reason_raw, str) and reason_raw.strip()
                else "Task requires explicit permission before execution can continue."
            )
            desired[HumanInteractionType.PERMISSION] = (
                reason,
                {
                    "source": self._TASK_SPEC_SOURCE,
                    "resume_token": f"permission-{task_id}",
                    "reason": reason,
                    "risk_level": task_spec.get("risk_level"),
                },
            )

        existing = self.list_by_task(
            task_id=task_id,
            interaction_types=self._TASK_SPEC_INTERACTION_TYPES,
        )
        task_spec_rows = [
            row
            for row in existing
            if isinstance(row.data, Mapping) and row.data.get("source") == self._TASK_SPEC_SOURCE
        ]

        for interaction_type in self._TASK_SPEC_INTERACTION_TYPES:
            interaction_rows = [
                row for row in task_spec_rows if row.interaction_type == interaction_type
            ]
            pending_rows = [
                row for row in interaction_rows if row.status == HumanInteractionStatus.PENDING
            ]
            resolved_rows = [
                row for row in interaction_rows if row.status == HumanInteractionStatus.RESOLVED
            ]
            active_rows = [
                row for row in interaction_rows if row.status != HumanInteractionStatus.CANCELLED
            ]
            desired_payload = desired.get(interaction_type)
            if desired_payload is None:
                for row in pending_rows:
                    row.status = HumanInteractionStatus.CANCELLED
                continue

            summary, data = desired_payload
            desired_resume_token = data.get("resume_token") if isinstance(data, Mapping) else None

            # If this logical checkpoint was already resolved (same resume token),
            # don't reopen it as pending due to wording drift in questions/summary.
            if isinstance(desired_resume_token, str) and desired_resume_token.strip():
                resolved_same_token = any(
                    isinstance(row.data, Mapping)
                    and row.data.get("resume_token") == desired_resume_token
                    for row in resolved_rows
                )
                if resolved_same_token:
                    for duplicate in pending_rows:
                        duplicate.status = HumanInteractionStatus.CANCELLED
                    continue

            if pending_rows:
                primary = pending_rows[0]
                primary.summary = summary
                primary.data = data
                primary.response_data = None
                primary.status = HumanInteractionStatus.PENDING
                for duplicate in pending_rows[1:]:
                    duplicate.status = HumanInteractionStatus.CANCELLED
                continue

            if active_rows:
                # Reuse the latest non-cancelled row to keep retries/idempotent sync
                # from creating duplicate interactions for the same TaskSpec signal.
                primary = active_rows[-1]
                if primary.status == HumanInteractionStatus.PENDING:
                    primary.summary = summary
                    primary.data = data
                    primary.response_data = None
                    continue

                # A resolved/rejected interaction should not suppress a materially
                # new operator checkpoint for the same task type.
                if primary.summary != summary or primary.data != data:
                    self.session.add(
                        HumanInteraction(
                            task_id=task_id,
                            interaction_type=interaction_type,
                            status=HumanInteractionStatus.PENDING,
                            summary=summary,
                            data=data,
                        )
                    )
                continue

            self.session.add(
                HumanInteraction(
                    task_id=task_id,
                    interaction_type=interaction_type,
                    status=HumanInteractionStatus.PENDING,
                    summary=summary,
                    data=data,
                )
            )

        self.session.flush()
        return self.list_by_task(
            task_id=task_id,
            interaction_types=self._TASK_SPEC_INTERACTION_TYPES,
        )


class InboundDeliveryRepository:
    """Persist and query webhook delivery dedupe claims."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        channel: str,
        delivery_id: str,
        task_id: str | None = None,
    ) -> InboundDelivery:
        delivery = InboundDelivery(
            channel=channel,
            delivery_id=delivery_id,
            task_id=task_id,
        )
        self.session.add(delivery)
        self.session.flush()
        return delivery

    def get_by_channel_delivery(
        self,
        *,
        channel: str,
        delivery_id: str,
    ) -> InboundDelivery | None:
        statement = select(InboundDelivery).where(
            InboundDelivery.channel == channel,
            InboundDelivery.delivery_id == delivery_id,
        )
        return self.session.scalar(statement)

    def attach_task_if_unassigned(
        self,
        *,
        channel: str,
        delivery_id: str,
        task_id: str,
    ) -> InboundDelivery | None:
        statement = (
            update(InboundDelivery)
            .where(
                InboundDelivery.channel == channel,
                InboundDelivery.delivery_id == delivery_id,
                InboundDelivery.task_id.is_(None),
            )
            .values(task_id=task_id)
            .returning(InboundDelivery.id)
        )
        result = self.session.execute(statement)
        updated_id = result.scalar_one_or_none()
        if updated_id is None:
            return None
        self.session.flush()
        return self.get_by_channel_delivery(channel=channel, delivery_id=delivery_id)


class WorkerRunRepository:
    """Persist and query worker runs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        task_id: str,
        session_id: str | None = None,
        worker_type: str | WorkerType,
        started_at: datetime,
        status: str | WorkerRunStatus,
        workspace_id: str | None = None,
        finished_at: datetime | None = None,
        summary: str | None = None,
        requested_permission: str | None = None,
        budget_usage: dict[str, Any] | None = None,
        verifier_outcome: dict[str, Any] | None = None,
        commands_run: list[dict[str, Any]] | None = None,
        files_changed_count: int = 0,
        files_changed: list[str] | None = None,
        artifact_index: list[dict[str, Any]] | None = None,
        retention_expires_at: datetime | None = None,
        worker_profile: str | None = None,
        runtime_mode: str | WorkerRuntimeMode | None = None,
    ) -> WorkerRun:
        worker_run = WorkerRun(
            task_id=task_id,
            session_id=session_id,
            worker_type=worker_type,
            workspace_id=workspace_id,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            worker_profile=worker_profile,
            runtime_mode=cast(WorkerRuntimeMode | None, runtime_mode),
            summary=summary,
            requested_permission=requested_permission,
            budget_usage=budget_usage,
            verifier_outcome=verifier_outcome,
            commands_run=commands_run,
            files_changed_count=files_changed_count,
            files_changed=files_changed,
            artifact_index=artifact_index,
            retention_expires_at=retention_expires_at,
        )
        self.session.add(worker_run)
        self.session.flush()
        return worker_run

    def get(self, run_id: str) -> WorkerRun | None:
        return self.session.get(WorkerRun, run_id)

    def list_by_task(self, task_id: str) -> list[WorkerRun]:
        statement = (
            select(WorkerRun)
            .where(WorkerRun.task_id == task_id)
            .order_by(WorkerRun.started_at.asc())
        )
        return list(self.session.scalars(statement))

    def list_retained_before(self, retention_expires_before: datetime) -> list[WorkerRun]:
        statement = (
            select(WorkerRun)
            .where(
                WorkerRun.retention_expires_at.is_not(None),
                WorkerRun.retention_expires_at <= retention_expires_before,
            )
            .order_by(WorkerRun.retention_expires_at.asc(), WorkerRun.started_at.asc())
        )
        return list(self.session.scalars(statement))

    def clear_artifact_index(self, run_id: str) -> WorkerRun | None:
        worker_run = self.get(run_id)
        if worker_run is None:
            return None

        worker_run.artifact_index = []
        self.session.flush()
        return worker_run

    def complete(
        self,
        *,
        run_id: str,
        status: str | WorkerRunStatus,
        finished_at: datetime,
        summary: str | None = None,
        requested_permission: str | None = None,
        budget_usage: dict[str, Any] | None = None,
        verifier_outcome: dict[str, Any] | None = None,
        commands_run: list[dict[str, Any]] | None = None,
        files_changed_count: int | None = None,
        files_changed: list[str] | None = None,
        artifact_index: list[dict[str, Any]] | None = None,
    ) -> WorkerRun | None:
        worker_run = self.get(run_id)
        if worker_run is None:
            return None

        worker_run.status = cast(WorkerRunStatus, status)
        worker_run.finished_at = finished_at
        if summary is not None:
            worker_run.summary = summary
        if requested_permission is not None:
            worker_run.requested_permission = requested_permission
        if budget_usage is not None:
            worker_run.budget_usage = budget_usage
        if verifier_outcome is not None:
            worker_run.verifier_outcome = verifier_outcome
        if commands_run is not None:
            worker_run.commands_run = commands_run
        if files_changed_count is not None:
            worker_run.files_changed_count = files_changed_count
        if files_changed is not None:
            worker_run.files_changed = files_changed
        if artifact_index is not None:
            worker_run.artifact_index = artifact_index
        self.session.flush()
        return worker_run

    def get_metrics(self, since: datetime | None = None) -> dict[str, Any]:
        """Aggregate worker execution, duration, and success metrics."""
        usage_stmt = select(WorkerRun.worker_type, func.count(WorkerRun.id)).group_by(
            WorkerRun.worker_type
        )
        if since:
            usage_stmt = usage_stmt.where(WorkerRun.started_at >= since)
        worker_usage = self.session.execute(usage_stmt).all()

        runtime_usage_stmt = select(WorkerRun.runtime_mode, func.count(WorkerRun.id)).group_by(
            WorkerRun.runtime_mode
        )
        if since:
            runtime_usage_stmt = runtime_usage_stmt.where(WorkerRun.started_at >= since)
        runtime_mode_usage = self.session.execute(runtime_usage_stmt).all()

        legacy_tool_loop_stmt = (
            select(WorkerRun.worker_type, func.count(WorkerRun.id))
            .where(
                WorkerRun.runtime_mode == WorkerRuntimeMode.TOOL_LOOP,
                WorkerRun.worker_type.in_((WorkerType.CODEX, WorkerType.GEMINI)),
            )
            .group_by(WorkerRun.worker_type)
        )
        if since:
            legacy_tool_loop_stmt = legacy_tool_loop_stmt.where(WorkerRun.started_at >= since)
        legacy_tool_loop_usage = self.session.execute(legacy_tool_loop_stmt).all()

        duration_stmt = select(
            func.avg(
                # NOTE: extract("epoch") is dialect-specific but handled via
                # SQLAlchemy translation for both Postgres and SQLite.
                func.extract("epoch", WorkerRun.finished_at)
                - func.extract("epoch", WorkerRun.started_at)
            ).label("avg_duration"),
            func.coalesce(
                func.sum(case((WorkerRun.status == WorkerRunStatus.SUCCESS, 1), else_=0)), 0
            ).label("success_count"),
            func.count(WorkerRun.id).label("total_count"),
        ).where(WorkerRun.finished_at.is_not(None))
        if since:
            duration_stmt = duration_stmt.where(WorkerRun.started_at >= since)
        duration_stats = self.session.execute(duration_stmt).one()

        return {
            "worker_usage": {
                (w.value if hasattr(w, "value") else str(w)): count for w, count in worker_usage
            },
            "runtime_mode_usage": {
                (m.value if hasattr(m, "value") else ("unknown" if m is None else str(m))): count
                for m, count in runtime_mode_usage
            },
            "legacy_tool_loop_usage": {
                (w.value if hasattr(w, "value") else str(w)): count
                for w, count in legacy_tool_loop_usage
            },
            "avg_duration_seconds": float(duration_stats.avg_duration or 0),
            "success_rate": (duration_stats.success_count / duration_stats.total_count)
            if duration_stats.total_count > 0
            else 0,
        }


class ArtifactRepository:
    """Persist and query run artifacts."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        run_id: str,
        artifact_type: str | ArtifactType,
        name: str,
        uri: str,
        artifact_metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        artifact = Artifact(
            run_id=run_id,
            artifact_type=artifact_type,
            name=name,
            uri=uri,
            artifact_metadata=artifact_metadata,
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    def list_by_run(self, run_id: str) -> list[Artifact]:
        statement = (
            select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at.asc())
        )
        return list(self.session.scalars(statement))

    def delete_by_run(self, run_id: str) -> int:
        statement = delete(Artifact).where(Artifact.run_id == run_id)
        result = self.session.execute(statement)
        self.session.flush()
        return int(getattr(result, "rowcount", 0) or 0)


class PersonalMemoryRepository:
    """Persist and query personal memory entries."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, *, user_id: str, memory_key: str) -> PersonalMemory | None:
        statement = select(PersonalMemory).where(
            PersonalMemory.user_id == user_id,
            PersonalMemory.memory_key == memory_key,
        )
        return self.session.scalar(statement)

    def list_by_user(self, user_id: str) -> list[PersonalMemory]:
        statement = (
            select(PersonalMemory)
            .where(PersonalMemory.user_id == user_id)
            .order_by(PersonalMemory.created_at.desc(), PersonalMemory.id.desc())
        )
        return list(self.session.scalars(statement))

    def list_all(
        self,
        *,
        user_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PersonalMemory]:
        statement = select(PersonalMemory)
        if user_id is not None:
            statement = statement.where(PersonalMemory.user_id == user_id)
        statement = (
            statement.order_by(PersonalMemory.created_at.desc(), PersonalMemory.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(statement))

    def upsert(
        self,
        *,
        user_id: str,
        memory_key: str,
        value: dict[str, Any],
        source: str | None | object = _UNSET,
        confidence: float | object = _UNSET,
        scope: str | None | object = _UNSET,
        last_verified_at: datetime | None | object = _UNSET,
        requires_verification: bool | object = _UNSET,
    ) -> PersonalMemory:
        memory_entry = self.get(user_id=user_id, memory_key=memory_key)
        if memory_entry is None:
            memory_entry = PersonalMemory(
                user_id=user_id,
                memory_key=memory_key,
                value=value,
            )
            try:
                with self.session.begin_nested():
                    self.session.add(memory_entry)
                    self.session.flush()
            except IntegrityError:
                memory_entry = self.get(user_id=user_id, memory_key=memory_key)
                if memory_entry is None:
                    raise
        _apply_memory_metadata(
            memory_entry,
            value=value,
            source=source,
            confidence=confidence,
            scope=scope,
            last_verified_at=last_verified_at,
            requires_verification=requires_verification,
        )
        self.session.flush()
        return memory_entry

    def delete(self, *, user_id: str, memory_key: str) -> bool:
        memory_entry = self.get(user_id=user_id, memory_key=memory_key)
        if memory_entry is None:
            return False

        self.session.delete(memory_entry)
        self.session.flush()
        return True


class ProjectMemoryRepository:
    """Persist and query project memory entries."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, *, repo_url: str, memory_key: str) -> ProjectMemory | None:
        statement = select(ProjectMemory).where(
            ProjectMemory.repo_url == repo_url,
            ProjectMemory.memory_key == memory_key,
        )
        return self.session.scalar(statement)

    def list_by_repo(self, repo_url: str) -> list[ProjectMemory]:
        statement = (
            select(ProjectMemory)
            .where(ProjectMemory.repo_url == repo_url)
            .order_by(ProjectMemory.created_at.desc(), ProjectMemory.id.desc())
        )
        return list(self.session.scalars(statement))

    def list_all(
        self,
        *,
        repo_url: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProjectMemory]:
        statement = select(ProjectMemory)
        if repo_url is not None:
            statement = statement.where(ProjectMemory.repo_url == repo_url)
        statement = (
            statement.order_by(ProjectMemory.created_at.desc(), ProjectMemory.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(statement))

    def upsert(
        self,
        *,
        repo_url: str,
        memory_key: str,
        value: dict[str, Any],
        source: str | None | object = _UNSET,
        confidence: float | object = _UNSET,
        scope: str | None | object = _UNSET,
        last_verified_at: datetime | None | object = _UNSET,
        requires_verification: bool | object = _UNSET,
    ) -> ProjectMemory:
        memory_entry = self.get(repo_url=repo_url, memory_key=memory_key)
        if memory_entry is None:
            memory_entry = ProjectMemory(
                repo_url=repo_url,
                memory_key=memory_key,
                value=value,
            )
            try:
                with self.session.begin_nested():
                    self.session.add(memory_entry)
                    self.session.flush()
            except IntegrityError:
                memory_entry = self.get(repo_url=repo_url, memory_key=memory_key)
                if memory_entry is None:
                    raise
        _apply_memory_metadata(
            memory_entry,
            value=value,
            source=source,
            confidence=confidence,
            scope=scope,
            last_verified_at=last_verified_at,
            requires_verification=requires_verification,
        )
        self.session.flush()
        return memory_entry

    def delete(self, *, repo_url: str, memory_key: str) -> bool:
        memory_entry = self.get(repo_url=repo_url, memory_key=memory_key)
        if memory_entry is None:
            return False

        self.session.delete(memory_entry)
        self.session.flush()
        return True


class TaskTimelineRepository:
    """Persist and query task timeline events (T-090)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        task_id: str,
        event_type: str | TimelineEventType,
        attempt_number: int = 0,
        sequence_number: int = 0,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> TaskTimelineEvent:
        event = TaskTimelineEvent(
            task_id=task_id,
            attempt_number=attempt_number,
            sequence_number=sequence_number,
            event_type=cast(TimelineEventType, event_type),
            message=message,
            payload=payload,
        )
        if created_at is not None:
            event.created_at = created_at
            event.updated_at = created_at
        self.session.add(event)
        self.session.flush()
        return event

    def list_by_task(self, task_id: str) -> list[TaskTimelineEvent]:
        statement = (
            select(TaskTimelineEvent)
            .where(TaskTimelineEvent.task_id == task_id)
            .order_by(
                TaskTimelineEvent.attempt_number.asc(), TaskTimelineEvent.sequence_number.asc()
            )
        )
        return list(self.session.scalars(statement))

    def count_by_attempt(self, task_id: str, attempt_number: int) -> int:
        """Count the number of timeline events persisted for a given task attempt."""
        return (
            self.session.scalar(
                select(func.count())
                .select_from(TaskTimelineEvent)
                .where(
                    TaskTimelineEvent.task_id == task_id,
                    TaskTimelineEvent.attempt_number == attempt_number,
                )
            )
            or 0
        )

    def create_next_for_attempt(
        self,
        *,
        task_id: str,
        attempt_number: int,
        event_type: str | TimelineEventType,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        max_retries: int = 3,
    ) -> TaskTimelineEvent:
        """Create the next sequence event for an attempt with bounded retry on conflicts."""
        tries = 0
        while True:
            sequence_number = self.count_by_attempt(task_id=task_id, attempt_number=attempt_number)
            try:
                with self.session.begin_nested():
                    event = self.create(
                        task_id=task_id,
                        attempt_number=attempt_number,
                        sequence_number=sequence_number,
                        event_type=event_type,
                        message=message,
                        payload=payload,
                        created_at=created_at,
                    )
                return event
            except IntegrityError:
                tries += 1
                if tries >= max_retries:
                    raise

    def create_batch(
        self,
        *,
        task_id: str,
        events: list[dict[str, Any]],
    ) -> None:
        """Bulk create timeline events for a task."""
        if not events:
            return

        now = utc_now()
        params = []
        for e in events:
            created_at = e.get("created_at") if e.get("created_at") is not None else now
            params.append(
                {
                    "id": uuid4().hex,
                    "task_id": task_id,
                    "attempt_number": e["attempt_number"],
                    "sequence_number": e["sequence_number"],
                    "event_type": e["event_type"],
                    "message": e.get("message"),
                    "payload": e.get("payload"),
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            )

        self.session.execute(insert(TaskTimelineEvent), params)
        self.session.flush()
