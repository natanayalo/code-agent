"""Submission, replay, and persistence-loading helpers for task execution."""

from __future__ import annotations

import copy
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.observability import capture_trace_context
from db.base import utc_now
from db.enums import TaskStatus, WorkerRunStatus
from db.models import Session as ConversationSession
from db.models import User
from orchestrator.execution_policy import (
    _deep_merge,
    _sanitize_submission_constraints,
    normalize_scout_submission,
)
from orchestrator.execution_types import (
    REPLAYABLE_STATUSES as _REPLAYABLE_STATUSES,
)
from orchestrator.execution_types import (
    RESERVED_INTERNAL_CONSTRAINT_KEYS as _RESERVED_INTERNAL_CONSTRAINT_KEYS,
)
from orchestrator.execution_types import (
    DeliveryKey,
    SubmissionSession,
    TaskReplayRequest,
    TaskReplayResult,
    TaskSubmission,
    TaskSubmissionValidationError,
    _PersistedTaskContext,
)
from orchestrator.task_spec import build_task_spec
from repositories import (
    HumanInteractionRepository,
    InboundDeliveryRepository,
    SessionRepository,
    TaskRepository,
    TaskTimelineRepository,
    UserRepository,
    WorkerRunRepository,
    session_scope,
)

logger = logging.getLogger("orchestrator.execution")


def _normalize_and_validate_submission(self: Any, submission: TaskSubmission) -> TaskSubmission:
    """Normalize execution overrides and validate profile selections before persistence."""
    normalized_profile_override = (
        submission.worker_profile_override.strip()
        if isinstance(submission.worker_profile_override, str)
        and submission.worker_profile_override.strip()
        else None
    )
    if (
        normalized_profile_override is not None
        and self.enable_worker_profiles
        and normalized_profile_override not in self.worker_profiles
    ):
        raise TaskSubmissionValidationError(
            "worker_profile_override must reference a configured worker profile "
            f"when profile routing is enabled. Unknown profile: '{normalized_profile_override}'."
        )
    if normalized_profile_override == submission.worker_profile_override:
        return submission
    return submission.model_copy(update={"worker_profile_override": normalized_profile_override})


def replay_task(
    self: Any,
    *,
    source_task_id: str,
    replay_request: TaskReplayRequest | None = None,
) -> TaskReplayResult:
    """Create a new task by replaying a prior terminal task with optional overrides."""
    source_snapshot = self.get_task(source_task_id)
    if source_snapshot is None:
        return TaskReplayResult(
            status="not_found",
            source_task_id=source_task_id,
            detail=f"Task '{source_task_id}' was not found.",
        )
    if source_snapshot.status not in _REPLAYABLE_STATUSES:
        return TaskReplayResult(
            status="not_replayable",
            source_task_id=source_task_id,
            detail=(
                f"Task '{source_task_id}' has status '{source_snapshot.status}' "
                "and cannot be replayed. Only terminal tasks "
                "(completed, failed, cancelled) are replayable."
            ),
        )

    loaded = self._load_submission_for_task(task_id=source_task_id)
    if loaded is None:
        return TaskReplayResult(
            status="not_found",
            source_task_id=source_task_id,
            detail=(
                f"Task '{source_task_id}' exists but its session or user "
                "could not be resolved for replay."
            ),
        )
    submission, _ = loaded
    updates: dict[str, Any] = {}
    if replay_request is not None:
        if replay_request.worker_override is not None:
            updates["worker_override"] = replay_request.worker_override
        if replay_request.worker_profile_override is not None:
            updates["worker_profile_override"] = replay_request.worker_profile_override
        if replay_request.constraints is not None:
            updates["constraints"] = _deep_merge(
                submission.constraints,
                replay_request.constraints,
                reserved_keys={"replayed_from"},
            )
        if replay_request.budget is not None:
            updates["budget"] = _deep_merge(
                submission.budget,
                replay_request.budget,
                reserved_keys={"replayed_from"},
            )
        if replay_request.secrets is not None:
            updates["secrets"] = dict(replay_request.secrets)

    if "constraints" in updates:
        updates["constraints"] = _sanitize_submission_constraints(
            updates["constraints"],
            reserved_keys=_RESERVED_INTERNAL_CONSTRAINT_KEYS,
        )

    base_constraints = updates.get("constraints", submission.constraints)
    existing_chain_raw = base_constraints.get("replayed_from")
    if isinstance(existing_chain_raw, str):
        existing_chain = [existing_chain_raw]
    elif isinstance(existing_chain_raw, list):
        existing_chain = list(existing_chain_raw)
    elif existing_chain_raw is None:
        existing_chain = []
    else:
        logger.warning(
            "Unexpected replayed_from type in task constraints; resetting provenance chain.",
            extra={"task_id": source_task_id, "actual_type": type(existing_chain_raw).__name__},
        )
        existing_chain = []

    existing_chain = [task_ref for task_ref in existing_chain if task_ref != source_task_id]
    if "constraints" not in updates:
        updates["constraints"] = copy.deepcopy(base_constraints)
    updates["constraints"]["replayed_from"] = [source_task_id, *existing_chain]

    submission = submission.model_copy(update=updates)
    task_snapshot, _ = self.create_task(submission)
    logger.info(
        "Replayed task created from source",
        extra={
            "source_task_id": source_task_id,
            "new_task_id": task_snapshot.task_id,
            "worker_override": submission.worker_override,
            "worker_profile_override": submission.worker_profile_override,
        },
    )
    return TaskReplayResult(
        status="created",
        task_snapshot=task_snapshot,
        source_task_id=source_task_id,
    )


def _persist_submission(
    self: Any,
    submission: TaskSubmission,
    *,
    status: TaskStatus,
    max_attempts: int,
    delivery_key: DeliveryKey | None = None,
) -> tuple[_PersistedTaskContext | None, str | None]:
    """Create or restore the session scaffolding for a submitted task."""
    now = utc_now()
    with session_scope(self.session_factory) as session:
        delivery_repo = InboundDeliveryRepository(session)
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        interaction_repo = HumanInteractionRepository(session)
        normalized_constraints, normalized_budget = normalize_scout_submission(
            submission.constraints, submission.budget
        )
        queue_lane = "scout" if normalized_constraints.get("task_type") == "scout" else "primary"

        sanitized_constraints = _sanitize_submission_constraints(
            normalized_constraints,
            reserved_keys=_RESERVED_INTERNAL_CONSTRAINT_KEYS,
        )
        persisted_constraints = dict(sanitized_constraints)
        if (
            isinstance(submission.worker_profile_override, str)
            and submission.worker_profile_override.strip()
        ):
            persisted_constraints["worker_profile_override"] = (
                submission.worker_profile_override.strip()
            )
        if submission.tools is not None:
            persisted_constraints["tools"] = submission.tools
        task_spec = build_task_spec(
            task_text=submission.task_text,
            repo_url=submission.repo_url,
            target_branch=submission.branch,
            constraints=sanitized_constraints,
        ).model_dump(mode="json")

        user = user_repo.get_by_external_user_id(submission.session.external_user_id)
        if user is None:
            user = self._create_or_get_user(
                session,
                user_repo,
                external_user_id=submission.session.external_user_id,
                display_name=submission.session.display_name,
            )

        conversation_session = session_repo.get_by_channel_thread(
            channel=submission.session.channel,
            external_thread_id=submission.session.external_thread_id,
        )
        if conversation_session is None:
            conversation_session = self._create_or_get_session(
                session,
                session_repo,
                user_id=user.id,
                channel=submission.session.channel,
                external_thread_id=submission.session.external_thread_id,
                last_seen_at=now,
            )
        session_repo.touch(session_id=conversation_session.id, seen_at=now)

        trace_context = capture_trace_context()
        task = task_repo.create(
            session_id=conversation_session.id,
            task_text=submission.task_text,
            repo_url=submission.repo_url,
            branch=submission.branch,
            callback_url=submission.callback_url,
            worker_override=submission.worker_override,
            budget=normalized_budget,
            secrets=dict(submission.secrets),
            task_spec=task_spec,
            trace_context=trace_context,
            constraints=persisted_constraints,
            secrets_encrypted=self.is_secret_encryption_active(),
            status=status,
            max_attempts=max(1, max_attempts),
            next_attempt_at=now,
            priority=submission.priority,
            queue_lane=queue_lane,
        )
        interaction_repo.sync_task_spec_flags(task_id=task.id, task_spec=task_spec)
        session_repo.set_active_task(session_id=conversation_session.id, active_task_id=task.id)
        if delivery_key is not None:
            duplicate_task_id = self._link_delivery_to_task(
                delivery_repo=delivery_repo,
                delivery_key=delivery_key,
                task_id=task.id,
            )
            if duplicate_task_id is not None:
                session.rollback()
                return None, duplicate_task_id

        return (
            _PersistedTaskContext(
                user_id=user.id,
                session_id=conversation_session.id,
                channel=conversation_session.channel,
                external_thread_id=conversation_session.external_thread_id,
                task_id=task.id,
                attempt_count=task.attempt_count,
                task_spec=task_spec,
            ),
            None,
        )


def _link_delivery_to_task(
    self: Any,
    *,
    delivery_repo: InboundDeliveryRepository,
    delivery_key: DeliveryKey,
    task_id: str,
) -> str | None:
    """Atomically bind a delivery key to a task or return the existing duplicate task id."""
    try:
        with delivery_repo.session.begin_nested():
            delivery_repo.create(
                channel=delivery_key.channel,
                delivery_id=delivery_key.delivery_id,
                task_id=task_id,
            )
        return None
    except IntegrityError:
        existing_delivery = delivery_repo.get_by_channel_delivery(
            channel=delivery_key.channel,
            delivery_id=delivery_key.delivery_id,
        )
        if existing_delivery is None:
            raise
        if existing_delivery.task_id is None:
            claimed_delivery = delivery_repo.attach_task_if_unassigned(
                channel=delivery_key.channel,
                delivery_id=delivery_key.delivery_id,
                task_id=task_id,
            )
            if claimed_delivery is not None:
                return None
            existing_delivery = delivery_repo.get_by_channel_delivery(
                channel=delivery_key.channel,
                delivery_id=delivery_key.delivery_id,
            )
            if existing_delivery is None:
                raise
            if existing_delivery.task_id is None:
                raise RuntimeError("Inbound delivery exists without a task_id after dedupe retry.")
        return existing_delivery.task_id


def _load_submission_for_task(
    self: Any,
    *,
    task_id: str,
) -> tuple[TaskSubmission, _PersistedTaskContext] | None:
    """Reconstruct a worker-run submission payload from persisted task/session state."""
    with session_scope(self.session_factory) as session:
        task_repo = TaskRepository(session)
        session_repo = SessionRepository(session)
        user_repo = UserRepository(session)

        task = task_repo.get(task_id)
        if task is None:
            return None
        conversation_session = session_repo.get(task.session_id)
        if conversation_session is None:
            return None
        user = user_repo.get(conversation_session.user_id)
        if user is None:
            return None
        task_constraints = dict(task.constraints or {})
        worker_profile_override = task_constraints.pop("worker_profile_override", None)
        submission = TaskSubmission(
            task_text=task.task_text,
            repo_url=task.repo_url,
            branch=task.branch,
            worker_override=task.worker_override,
            worker_profile_override=(
                worker_profile_override.strip()
                if isinstance(worker_profile_override, str) and worker_profile_override.strip()
                else None
            ),
            constraints=task_constraints,
            budget=dict(task.budget or {}),
            secrets=dict(task.secrets or {}),
            callback_url=task.callback_url,
            tools=task_constraints.get("tools"),
            priority=task.priority,
            session=SubmissionSession(
                channel=conversation_session.channel,
                external_user_id=user.external_user_id or "unknown",
                external_thread_id=conversation_session.external_thread_id,
                display_name=user.display_name,
            ),
        )
        runs = WorkerRunRepository(session).list_by_task(task_id)
        last_run_dispatch = None
        last_run_result = None
        if runs:
            last_run = runs[-1]
            last_run_dispatch = {
                "run_id": last_run.id,
                "worker_type": (
                    last_run.worker_type.value
                    if last_run.worker_type and hasattr(last_run.worker_type, "value")
                    else str(last_run.worker_type)
                    if last_run.worker_type
                    else None
                ),
                "worker_profile": last_run.worker_profile,
                "runtime_mode": (
                    last_run.runtime_mode.value
                    if last_run.runtime_mode and hasattr(last_run.runtime_mode, "value")
                    else str(last_run.runtime_mode)
                    if last_run.runtime_mode
                    else None
                ),
                "workspace_id": last_run.workspace_id,
            }
            failure_kind = (
                last_run.verifier_outcome.get("failure_kind") if last_run.verifier_outcome else None
            )
            if not failure_kind and last_run.status == WorkerRunStatus.FAILURE:
                failure_kind = "unknown"
            last_run_result = {
                "status": last_run.status.value
                if hasattr(last_run.status, "value")
                else str(last_run.status),
                "summary": last_run.summary,
                "failure_kind": failure_kind,
                "workspace_id": last_run.workspace_id,
                "requested_permission": last_run.requested_permission,
                "budget_usage": last_run.budget_usage,
                "commands_run": last_run.commands_run or [],
                "files_changed": last_run.files_changed or [],
                "test_results": [],
                "artifacts": [],
            }

        timeline_events = TaskTimelineRepository(session).list_by_task(task_id)
        serialized_events = [
            {
                "event_type": event.event_type.value
                if hasattr(event.event_type, "value")
                else str(event.event_type),
                "attempt_number": event.attempt_number,
                "sequence_number": event.sequence_number,
                "message": event.message,
                "payload": event.payload,
                "created_at": event.created_at.isoformat() if event.created_at else None,
            }
            for event in timeline_events
        ]
        persisted = _PersistedTaskContext(
            user_id=user.id,
            session_id=conversation_session.id,
            channel=conversation_session.channel,
            external_thread_id=conversation_session.external_thread_id,
            task_id=task.id,
            attempt_count=task.attempt_count,
            task_spec=dict(task.task_spec) if isinstance(task.task_spec, dict) else None,
            trace_context=dict(task.trace_context or {}),
            last_run_dispatch=last_run_dispatch,
            last_run_result=last_run_result,
            timeline_events=serialized_events,
        )
        return submission, persisted


def _mark_task_in_progress(self: Any, *, task_id: str) -> None:
    with session_scope(self.session_factory) as session:
        TaskRepository(session).update_status(task_id=task_id, status=TaskStatus.IN_PROGRESS)


def _mark_task_failed(self: Any, *, task_id: str) -> None:
    with session_scope(self.session_factory) as session:
        TaskRepository(session).update_status(task_id=task_id, status=TaskStatus.FAILED)


def _release_task_success(self: Any, *, task_id: str) -> None:
    with session_scope(self.session_factory) as session:
        TaskRepository(session).release_success(task_id=task_id)


def _release_task_failure(self: Any, *, task_id: str, worker_id: str) -> None:
    with session_scope(self.session_factory) as session:
        TaskRepository(session).release_failure(
            task_id=task_id,
            worker_id=worker_id,
            now=utc_now(),
            retry_backoff_seconds=15,
        )


def _release_task_terminal_failure(
    self: Any,
    *,
    task_id: str,
    worker_id: str,
    status: TaskStatus = TaskStatus.FAILED,
) -> None:
    with session_scope(self.session_factory) as session:
        TaskRepository(session).release_terminal_failure(
            task_id=task_id,
            worker_id=worker_id,
            status=status,
        )


def _record_task_attempt_error(self: Any, *, task_id: str, error: str) -> None:
    with session_scope(self.session_factory) as session:
        TaskRepository(session).record_attempt_error(task_id=task_id, error_text=error)


def _heartbeat_task_lease(
    self: Any,
    *,
    task_id: str,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    with session_scope(self.session_factory) as session:
        return TaskRepository(session).heartbeat_lease(
            task_id=task_id,
            worker_id=worker_id,
            now=utc_now(),
            lease_seconds=lease_seconds,
        )


def _create_or_get_user(
    self: Any,
    session: Session,
    user_repo: UserRepository,
    *,
    external_user_id: str,
    display_name: str | None,
) -> User:
    try:
        with session.begin_nested():
            return user_repo.create(
                external_user_id=external_user_id,
                display_name=display_name,
            )
    except IntegrityError:
        existing_user = user_repo.get_by_external_user_id(external_user_id)
        if existing_user is None:
            raise
        return existing_user


def _create_or_get_session(
    self: Any,
    session: Session,
    session_repo: SessionRepository,
    *,
    user_id: str,
    channel: str,
    external_thread_id: str,
    last_seen_at: datetime,
) -> ConversationSession:
    try:
        with session.begin_nested():
            return session_repo.create(
                user_id=user_id,
                channel=channel,
                external_thread_id=external_thread_id,
                last_seen_at=last_seen_at,
            )
    except IntegrityError:
        existing_session = session_repo.get_by_channel_thread(
            channel=channel,
            external_thread_id=external_thread_id,
        )
        if existing_session is None:
            raise
        return existing_session
