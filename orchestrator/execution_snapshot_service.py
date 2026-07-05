"""Snapshot, listing, retention, and metrics helpers for task execution."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.base import utc_now
from db.enums import (
    ArtifactType,
    HumanInteractionStatus,
    MemoryProposalStatus,
    TaskStatus,
    WorkerRunStatus,
)
from db.models import (
    ExecutionPlan,
    HumanInteraction,
    MemoryAdmissionDecision,
    MemoryObservation,
    MemoryProposal,
    PersonalMemory,
    ProjectMemory,
    Task,
    WorkerRun,
)
from db.models import Session as ConversationSession
from orchestrator.execution_serialization import _enum_value, _get_trace_id_from_context
from orchestrator.execution_tracing import _get_phoenix_url
from orchestrator.execution_types import (
    ArtifactSnapshot,
    ExecutionPlanNodeSnapshot,
    ExecutionPlanSnapshot,
    KnowledgeBaseStatsSnapshot,
    MemoryAdmissionDecisionSnapshot,
    MemoryInventoryCountSnapshot,
    MemoryObservationSnapshot,
    MemoryProposalCreateRequest,
    MemoryProposalSnapshot,
    OperationalMetrics,
    PersonalMemorySnapshot,
    PersonalMemoryUpsertRequest,
    ProjectMemorySnapshot,
    ProjectMemoryUpsertRequest,
    SessionSnapshot,
    SessionWorkingContextSnapshot,
    TaskClaim,
    TaskSnapshot,
    TaskSummarySnapshot,
    TaskTimelineEventSnapshot,
    WorkerRunSnapshot,
)
from orchestrator.state import TaskSpec
from repositories import (
    MemoryAdmissionDecisionRepository,
    MemoryProposalRepository,
    ObservationRepository,
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    SessionRepository,
    TaskRepository,
    WorkerRunRepository,
    session_scope,
)

logger = logging.getLogger("orchestrator.execution")


def claim_next_task(self: Any, *, worker_id: str, lease_seconds: int) -> TaskClaim | None:
    """Claim one queued task for worker execution."""
    self.ensure_worker_node(worker_id=worker_id)
    with session_scope(self.session_factory) as session:
        task_repo = TaskRepository(session)
        task = task_repo.claim_next(
            worker_id=worker_id,
            now=utc_now(),
            lease_seconds=lease_seconds,
        )
        if task is None:
            return None
        return TaskClaim(
            task_id=task.id,
            attempt_count=task.attempt_count,
            max_attempts=task.max_attempts,
        )


def is_execution_busy(self: Any) -> bool:
    """Return True if any tasks are currently pending or in progress across any queue lane."""
    with session_scope(self.session_factory) as session:
        task_repo = TaskRepository(session)
        return task_repo.is_execution_busy()


def get_task(self: Any, task_id: str) -> TaskSnapshot | None:
    """Load the current persisted task state with full timeline and latest run."""
    with session_scope(self.session_factory) as session:
        statement = (
            select(Task)
            .where(Task.id == task_id)
            .options(
                selectinload(Task.timeline_events),
                selectinload(Task.human_interactions),
                selectinload(Task.worker_runs).selectinload(WorkerRun.artifacts),
                selectinload(Task.execution_plan).selectinload(ExecutionPlan.nodes),
            )
        )
        task = session.scalar(statement)
        if task is None:
            return None
        return self._map_task_to_snapshot(task)


def list_tasks(
    self: Any,
    *,
    session_id: str | None = None,
    status: str | TaskStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[TaskSummarySnapshot]:
    """List tasks with optional filtering and pagination using summary views."""
    with session_scope(self.session_factory) as session:
        task_repo = TaskRepository(session)
        tasks = task_repo.list_all(
            session_id=session_id,
            status=status,
            limit=limit,
            offset=offset,
            preload_history=False,
        )
        return [self._map_task_to_summary(task) for task in tasks]


def list_sessions(
    self: Any,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[SessionSnapshot]:
    """List sessions with pagination."""
    with session_scope(self.session_factory) as session:
        session_repo = SessionRepository(session)
        sessions = session_repo.list_all(limit=limit, offset=offset)
        return [self._map_session_to_snapshot(s) for s in sessions]


def get_session(self: Any, session_id: str) -> SessionSnapshot | None:
    """Load the current persisted session state."""
    with session_scope(self.session_factory) as session:
        session_repo = SessionRepository(session)
        conversation_session = session_repo.get(session_id)
        if conversation_session is None:
            return None
        return self._map_session_to_snapshot(conversation_session)


def list_personal_memory(
    self: Any,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[PersonalMemorySnapshot]:
    """List persisted operator-global personal memory entries."""
    with session_scope(self.session_factory) as session:
        memory_repo = PersonalMemoryRepository(session)
        memories = memory_repo.list_all(limit=limit, offset=offset)
        return [self._map_personal_memory_to_snapshot(memory) for memory in memories]


def get_knowledge_base_stats(
    self: Any,
    *,
    repo_url: str | None = None,
) -> KnowledgeBaseStatsSnapshot:
    """Return exact skeptical-memory inventory counts for dashboard browse surfaces."""
    normalized_repo_url = _optional_scope(repo_url)
    with session_scope(self.session_factory) as session:
        personal_repo = PersonalMemoryRepository(session)
        personal_stats = _memory_count_snapshot(personal_repo.count_all())

        project_repo = ProjectMemoryRepository(session)
        project_stats = (
            _memory_count_snapshot(project_repo.count_all(repo_url=normalized_repo_url))
            if normalized_repo_url
            else None
        )
        project_global_stats = _memory_count_snapshot(project_repo.count_all())

        return KnowledgeBaseStatsSnapshot(
            personal=personal_stats,
            project=project_stats,
            project_global=project_global_stats,
        )


def _optional_scope(value: str | None) -> str | None:
    """Normalize blank dashboard scope parameters to omitted scopes."""
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def search_personal_memory(
    self: Any,
    *,
    query: str,
    limit: int = 20,
) -> list[PersonalMemorySnapshot]:
    """Search persisted operator-global personal memory entries."""
    with session_scope(self.session_factory) as session:
        memory_repo = PersonalMemoryRepository(session)
        results = memory_repo.search(query=query, limit=limit)
        return [
            self._map_personal_memory_to_snapshot(result.memory, headline=result.headline)
            for result in results
        ]


def upsert_personal_memory(
    self: Any,
    payload: PersonalMemoryUpsertRequest,
) -> PersonalMemorySnapshot:
    """Create or update one personal memory entry."""
    with session_scope(self.session_factory) as session:
        memory_repo = PersonalMemoryRepository(session)
        upsert_kwargs: dict[str, Any] = {
            "memory_key": payload.memory_key,
            "value": payload.value,
        }
        for field_name in (
            "source",
            "confidence",
            "scope",
            "last_verified_at",
            "requires_verification",
        ):
            if field_name in payload.model_fields_set:
                upsert_kwargs[field_name] = getattr(payload, field_name)

        memory = memory_repo.upsert(**upsert_kwargs)
        return self._map_personal_memory_to_snapshot(memory)


def delete_personal_memory(self: Any, *, memory_key: str) -> bool:
    """Delete one personal memory entry by key."""
    with session_scope(self.session_factory) as session:
        memory_repo = PersonalMemoryRepository(session)
        return memory_repo.delete(memory_key=memory_key)


def list_project_memory(
    self: Any,
    *,
    repo_url: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ProjectMemorySnapshot]:
    """List persisted project memory entries with optional repo filtering."""
    with session_scope(self.session_factory) as session:
        memory_repo = ProjectMemoryRepository(session)
        memories = memory_repo.list_all(repo_url=repo_url, limit=limit, offset=offset)
        return [self._map_project_memory_to_snapshot(memory) for memory in memories]


def search_project_memory(
    self: Any,
    *,
    repo_url: str,
    query: str,
    limit: int = 20,
) -> list[ProjectMemorySnapshot]:
    """Search persisted repository memory entries."""
    with session_scope(self.session_factory) as session:
        memory_repo = ProjectMemoryRepository(session)
        results = memory_repo.search(repo_url=repo_url, query=query, limit=limit)
        return [
            self._map_project_memory_to_snapshot(result.memory, headline=result.headline)
            for result in results
        ]


def upsert_project_memory(
    self: Any,
    payload: ProjectMemoryUpsertRequest,
) -> ProjectMemorySnapshot:
    """Create or update one project memory entry."""
    with session_scope(self.session_factory) as session:
        memory_repo = ProjectMemoryRepository(session)
        upsert_kwargs: dict[str, Any] = {
            "repo_url": payload.repo_url,
            "memory_key": payload.memory_key,
            "value": payload.value,
        }
        for field_name in (
            "source",
            "confidence",
            "scope",
            "last_verified_at",
            "requires_verification",
        ):
            if field_name in payload.model_fields_set:
                upsert_kwargs[field_name] = getattr(payload, field_name)

        memory = memory_repo.upsert(**upsert_kwargs)
        return self._map_project_memory_to_snapshot(memory)


def delete_project_memory(self: Any, *, repo_url: str, memory_key: str) -> bool:
    """Delete one project memory entry by key."""
    with session_scope(self.session_factory) as session:
        memory_repo = ProjectMemoryRepository(session)
        return memory_repo.delete(repo_url=repo_url, memory_key=memory_key)


def create_memory_proposal(
    self: Any,
    payload: MemoryProposalCreateRequest,
) -> MemoryProposalSnapshot:
    """Create one reviewable memory proposal."""
    with session_scope(self.session_factory) as session:
        proposal = MemoryProposalRepository(session).create(
            category=payload.category,
            repo_url=payload.repo_url,
            memory_key=payload.memory_key,
            value=payload.value,
            source=payload.source,
            confidence=payload.confidence,
            scope=payload.scope,
            requires_verification=payload.requires_verification,
            title=payload.title,
            summary=payload.summary,
            evidence=payload.evidence,
            task_id=payload.task_id,
            session_id=payload.session_id,
        )
        return self._map_memory_proposal_to_snapshot(proposal)


def list_memory_proposals(
    self: Any,
    *,
    status: str | MemoryProposalStatus | Sequence[str | MemoryProposalStatus] | None = None,
    category: str | None = None,
    repo_url: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[MemoryProposalSnapshot]:
    """List reviewable memory proposals with optional filters."""
    with session_scope(self.session_factory) as session:
        proposals = MemoryProposalRepository(session).list(
            status=status,
            category=category,
            repo_url=repo_url,
            task_id=task_id,
            session_id=session_id,
            limit=limit,
            offset=offset,
        )
        return [self._map_memory_proposal_to_snapshot(proposal) for proposal in proposals]


def list_memory_observations(
    self: Any,
    *,
    repo_url: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    event_type: str | None = None,
    admission_status: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[MemoryObservationSnapshot]:
    """List persisted episodic observations with optional filters."""
    with session_scope(self.session_factory) as session:
        observations = ObservationRepository(session).list(
            repo_url=_optional_scope(repo_url),
            task_id=_optional_scope(task_id),
            session_id=_optional_scope(session_id),
            source=_optional_scope(source),
            event_type=_optional_scope(event_type),
            admission_status=_optional_scope(admission_status),
            query=query,
            limit=limit,
            offset=offset,
        )
        decision_rows = MemoryAdmissionDecisionRepository(session).list(
            repo_url=_optional_scope(repo_url),
            task_id=_optional_scope(task_id),
            session_id=_optional_scope(session_id),
            limit=max(limit + offset, 200),
            offset=0,
        )
        decisions_by_observation_id = {
            row.source_observation_id: row
            for row in decision_rows
            if row.source_observation_id is not None
        }
        return [
            self._map_memory_observation_to_snapshot(
                observation,
                decision=decisions_by_observation_id.get(observation.id),
            )
            for observation in observations
        ]


def get_memory_observation(
    self: Any,
    observation_id: str,
) -> MemoryObservationSnapshot | None:
    """Fetch one persisted episodic observation with lineage details."""
    with session_scope(self.session_factory) as session:
        observation = ObservationRepository(session).get(observation_id)
        if observation is None:
            return None
        decision_rows = MemoryAdmissionDecisionRepository(session).list(
            source_observation_id=observation_id,
            limit=1,
            offset=0,
        )
        decision = decision_rows[0] if decision_rows else None
        return self._map_memory_observation_to_snapshot(observation, decision=decision)


def list_memory_admission_decisions(
    self: Any,
    *,
    repo_url: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    decision: str | None = None,
    source_observation_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[MemoryAdmissionDecisionSnapshot]:
    """List inspectable memory-admission decisions with lineage filters."""
    with session_scope(self.session_factory) as session:
        decisions = MemoryAdmissionDecisionRepository(session).list(
            repo_url=_optional_scope(repo_url),
            task_id=_optional_scope(task_id),
            session_id=_optional_scope(session_id),
            decision=_optional_scope(decision),
            source_observation_id=_optional_scope(source_observation_id),
            limit=limit,
            offset=offset,
        )
        observation_repo = ObservationRepository(session)
        observation_ids = {
            row.source_observation_id for row in decisions if row.source_observation_id is not None
        }
        observations_by_id = {
            observation_id: observation_repo.get(observation_id)
            for observation_id in observation_ids
        }
        task_ids = {row.task_id for row in decisions if row.task_id is not None}
        tasks_by_id = (
            {
                task.id: task
                for task in session.scalars(select(Task).where(Task.id.in_(task_ids))).all()
            }
            if task_ids
            else {}
        )
        return [
            self._map_memory_admission_decision_to_snapshot(
                row,
                repo_url=_decision_repo_url(
                    row,
                    observation=(
                        observations_by_id.get(row.source_observation_id)
                        if row.source_observation_id is not None
                        else None
                    ),
                    task=tasks_by_id.get(row.task_id) if row.task_id is not None else None,
                ),
            )
            for row in decisions
        ]


def accept_memory_proposal(
    self: Any,
    proposal_id: str,
) -> tuple[str, MemoryProposalSnapshot | None, str | None]:
    """Accept a memory proposal and upsert its target skeptical-memory row."""
    with session_scope(self.session_factory) as session:
        status, proposal, _memory, detail = MemoryProposalRepository(session).accept(proposal_id)
        if proposal is None:
            return status, None, detail
        return status, self._map_memory_proposal_to_snapshot(proposal), detail


def reject_memory_proposal(
    self: Any,
    proposal_id: str,
) -> tuple[str, MemoryProposalSnapshot | None, str | None]:
    """Reject a memory proposal without writing durable memory."""
    with session_scope(self.session_factory) as session:
        status, proposal, detail = MemoryProposalRepository(session).reject(proposal_id)
        if proposal is None:
            return status, None, detail
        return status, self._map_memory_proposal_to_snapshot(proposal), detail


def _map_execution_plan_to_snapshot(execution_plan: Any) -> ExecutionPlanSnapshot | None:
    if execution_plan is None:
        return None
    return ExecutionPlanSnapshot(
        plan_id=execution_plan.id,
        task_id=execution_plan.task_id,
        created_at=execution_plan.created_at,
        updated_at=execution_plan.updated_at,
        nodes=[
            ExecutionPlanNodeSnapshot(
                node_id=node.node_id,
                depends_on=node.depends_on,
                status=cast(Any, _enum_value(node.status) or "pending"),
                goal=node.goal,
                acceptance_criteria=node.acceptance_criteria,
                assigned_worker_profile=node.assigned_worker_profile,
                budget=node.budget,
                validation_commands=node.validation_commands,
                artifacts=node.artifacts,
                blocker_interaction_id=node.blocker_interaction_id,
                retry_count=node.retry_count,
                started_at=node.started_at,
                finished_at=node.finished_at,
                created_at=node.created_at,
                updated_at=node.updated_at,
            )
            for node in getattr(execution_plan, "nodes", [])
        ],
    )


def _map_task_to_snapshot(self: Any, task: Task) -> TaskSnapshot:
    latest_run_snapshot: WorkerRunSnapshot | None = None
    latest_run_obj: WorkerRun | None = None
    pending_interactions = self._pending_interaction_snapshots(task)

    if task.worker_runs:
        latest_run_obj = max(task.worker_runs, key=lambda row: (row.started_at, row.id))
        latest_run_snapshot = WorkerRunSnapshot(
            run_id=latest_run_obj.id,
            session_id=latest_run_obj.session_id,
            worker_type=_enum_value(latest_run_obj.worker_type) or "unknown",
            worker_profile=latest_run_obj.worker_profile,
            runtime_mode=_enum_value(latest_run_obj.runtime_mode),
            workspace_id=latest_run_obj.workspace_id,
            status=_enum_value(latest_run_obj.status) or WorkerRunStatus.ERROR.value,
            started_at=latest_run_obj.started_at,
            finished_at=latest_run_obj.finished_at,
            summary=latest_run_obj.summary,
            requested_permission=latest_run_obj.requested_permission,
            budget_usage=latest_run_obj.budget_usage,
            verifier_outcome=self._ensure_verifier_outcome_ids(latest_run_obj.verifier_outcome),
            commands_run=[
                {"id": command.get("id") or f"legacy-{idx}", **command}
                for idx, command in enumerate(latest_run_obj.commands_run or [])
                if isinstance(command, dict)
            ],
            files_changed_count=latest_run_obj.files_changed_count,
            files_changed=list(latest_run_obj.files_changed or []),
            artifact_index=[
                {"id": entry.get("id") or entry.get("uri") or f"idx-{idx}", **entry}
                for idx, entry in enumerate(latest_run_obj.artifact_index or [])
                if isinstance(entry, dict)
            ],
            delivery_metadata=cast(Any, latest_run_obj.delivery_metadata),
            artifacts=[
                ArtifactSnapshot(
                    artifact_id=artifact.id,
                    artifact_type=_enum_value(artifact.artifact_type)
                    or ArtifactType.RESULT_SUMMARY.value,
                    name=artifact.name,
                    uri=artifact.uri,
                    artifact_metadata=artifact.artifact_metadata,
                )
                for artifact in (
                    latest_run_obj.artifacts if "artifacts" in latest_run_obj.__dict__ else []
                )
            ],
        )

    summary = self._map_task_to_summary(task, latest_run=latest_run_obj)
    execution_plan_snapshot = (
        _map_execution_plan_to_snapshot(task.execution_plan)
        if "execution_plan" in task.__dict__
        else None
    )
    timeline = [
        TaskTimelineEventSnapshot(
            id=event.id,
            event_type=_enum_value(event.event_type) or "unknown",
            attempt_number=event.attempt_number,
            sequence_number=event.sequence_number,
            message=event.message,
            payload=event.payload,
            created_at=event.created_at,
        )
        for event in (task.timeline_events if "timeline_events" in task.__dict__ else [])
    ]

    return TaskSnapshot(
        **summary.model_dump(),
        task_spec=TaskSpec.model_validate(task.task_spec)
        if isinstance(task.task_spec, dict)
        else None,
        execution_plan=execution_plan_snapshot,
        latest_run=latest_run_snapshot,
        pending_interactions=pending_interactions,
        timeline=timeline,
    )


def _map_task_to_summary(
    self: Any,
    task: Task,
    *,
    latest_run: WorkerRun | None = None,
) -> TaskSummarySnapshot:
    latest_run_id = getattr(task, "_latest_run_id", None)
    latest_run_status = _enum_value(getattr(task, "_latest_run_status", None))
    latest_run_worker = _enum_value(getattr(task, "_latest_run_worker", None))
    latest_run_requested_permission = getattr(task, "_latest_run_requested_permission", None)
    pending_interaction_count = getattr(task, "_pending_interaction_count", None)

    if latest_run_id is None:
        if latest_run:
            latest_run_id = latest_run.id
            latest_run_status = _enum_value(latest_run.status)
            latest_run_worker = _enum_value(latest_run.worker_type)
            latest_run_requested_permission = latest_run.requested_permission
        elif "worker_runs" in task.__dict__ and task.worker_runs:
            latest_run = max(task.worker_runs, key=lambda row: (row.started_at, row.id))
            latest_run_id = latest_run.id
            latest_run_status = _enum_value(latest_run.status)
            latest_run_worker = _enum_value(latest_run.worker_type)
            latest_run_requested_permission = latest_run.requested_permission

    if pending_interaction_count is None:
        pending_interaction_count = (
            self._count_pending_interactions(task) if "human_interactions" in task.__dict__ else 0
        )

    constraints = task.constraints or {}
    approval_checkpoint = constraints.get("approval")
    approval_status = None
    approval_type = None
    approval_reason = None
    if isinstance(approval_checkpoint, dict):
        approval_status = approval_checkpoint.get("status")
        approval_type = approval_checkpoint.get("approval_type")
        approval_reason = approval_checkpoint.get("reason")

    trace_id = _get_trace_id_from_context(task.trace_context)
    return TaskSummarySnapshot(
        task_id=task.id,
        session_id=task.session_id,
        status=_enum_value(task.status) or TaskStatus.FAILED.value,
        task_text=task.task_text,
        repo_url=task.repo_url,
        branch=task.branch,
        priority=task.priority,
        chosen_worker=_enum_value(task.chosen_worker),
        chosen_profile=task.chosen_profile,
        runtime_mode=_enum_value(task.runtime_mode),
        route_reason=task.route_reason,
        constraints=dict(task.constraints) if isinstance(task.constraints, dict) else {},
        created_at=task.created_at,
        updated_at=task.updated_at,
        latest_run_id=latest_run_id,
        latest_run_status=latest_run_status,
        latest_run_worker=latest_run_worker,
        latest_run_requested_permission=latest_run_requested_permission,
        pending_interaction_count=int(pending_interaction_count or 0),
        last_error=task.last_error,
        approval_status=approval_status,
        approval_type=approval_type,
        approval_reason=approval_reason,
        trace_id=trace_id,
        trace_url=_get_phoenix_url(trace_id),
        repair_for_task_id=task.repair_for_task_id,
    )


def _is_pending_interaction(interaction: HumanInteraction) -> bool:
    return _enum_value(interaction.status) == HumanInteractionStatus.PENDING.value


def _map_human_interaction_snapshot(interaction: HumanInteraction):  # type: ignore[no-untyped-def]
    from orchestrator.execution_types import HumanInteractionSnapshot

    return HumanInteractionSnapshot(
        interaction_id=interaction.id,
        interaction_type=_enum_value(interaction.interaction_type) or "unknown",
        status=_enum_value(interaction.status) or "unknown",
        summary=interaction.summary,
        decision_key=interaction.decision_key,
        hitl_mode=_enum_value(interaction.hitl_mode) or "require_approval",
        data=dict(interaction.data or {}),
        response_data=(
            dict(interaction.response_data or {}) if interaction.response_data is not None else None
        ),
        created_at=interaction.created_at,
        updated_at=interaction.updated_at,
    )


def _pending_interaction_snapshots(self: Any, task: Task):  # type: ignore[no-untyped-def]
    interactions = task.human_interactions if "human_interactions" in task.__dict__ else []
    pending_interactions = [
        interaction for interaction in interactions if _is_pending_interaction(interaction)
    ]
    return [
        _map_human_interaction_snapshot(interaction)
        for interaction in sorted(pending_interactions, key=lambda row: (row.created_at, row.id))
    ]


def _count_pending_interactions(self: Any, task: Task) -> int:
    return sum(1 for interaction in task.human_interactions if _is_pending_interaction(interaction))


def _ensure_verifier_outcome_ids(self: Any, outcome: Any) -> Any:
    """Inject stable IDs into verifier outcome items if missing."""
    if not isinstance(outcome, dict):
        return outcome
    items = outcome.get("items")
    if not isinstance(items, list):
        return outcome

    new_items = []
    for idx, item in enumerate(items):
        if isinstance(item, dict) and not item.get("id"):
            label = item.get("label", "item")
            status = item.get("status", "unknown")
            new_items.append({"id": f"v-{idx}-{label}-{status}", **item})
        else:
            new_items.append(item)
    return {**outcome, "items": new_items}


def _map_session_to_snapshot(
    self: Any, conversation_session: ConversationSession
) -> SessionSnapshot:
    """Map a conversation session row to its snapshot model."""
    working_context: SessionWorkingContextSnapshot | None = None
    if (
        "session_state" in conversation_session.__dict__
        and conversation_session.session_state is not None
    ):
        state = conversation_session.session_state
        working_context = SessionWorkingContextSnapshot(
            active_goal=state.active_goal,
            decisions_made=dict(state.decisions_made or {}),
            identified_risks=dict(state.identified_risks or {}),
            files_touched=list(state.files_touched or []),
            updated_at=state.updated_at,
        )

    return SessionSnapshot(
        session_id=conversation_session.id,
        user_id=conversation_session.user_id,
        channel=conversation_session.channel,
        external_thread_id=conversation_session.external_thread_id,
        active_task_id=conversation_session.active_task_id,
        status=_enum_value(conversation_session.status) or "active",
        last_seen_at=conversation_session.last_seen_at,
        created_at=conversation_session.created_at,
        updated_at=conversation_session.updated_at,
        working_context=working_context,
    )


def _map_personal_memory_to_snapshot(
    memory: PersonalMemory,
    *,
    headline: str | None = None,
) -> PersonalMemorySnapshot:
    return PersonalMemorySnapshot(
        memory_id=memory.id,
        memory_key=memory.memory_key,
        value=dict(memory.value or {}),
        headline=headline,
        source=memory.source,
        confidence=memory.confidence,
        scope=memory.scope,
        last_verified_at=memory.last_verified_at,
        requires_verification=memory.requires_verification,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


def _memory_count_snapshot(counts: tuple[int, int]) -> MemoryInventoryCountSnapshot:
    total, requires_verification = counts
    return MemoryInventoryCountSnapshot(
        total=total,
        requires_verification=requires_verification,
    )


def _map_memory_proposal_to_snapshot(proposal: MemoryProposal) -> MemoryProposalSnapshot:
    return MemoryProposalSnapshot(
        proposal_id=proposal.id,
        category=proposal.category,
        repo_url=proposal.repo_url,
        memory_key=proposal.memory_key,
        value=dict(proposal.value or {}),
        source=proposal.source,
        confidence=proposal.confidence,
        scope=proposal.scope,
        requires_verification=proposal.requires_verification,
        status=proposal.status,
        title=proposal.title,
        summary=proposal.summary,
        evidence=dict(proposal.evidence) if proposal.evidence else None,
        task_id=proposal.task_id,
        session_id=proposal.session_id,
        source_observation_id=proposal.source_observation_id,
        accepted_memory_id=proposal.accepted_memory_id,
        reviewed_at=proposal.reviewed_at,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
    )


def _map_memory_observation_to_snapshot(
    observation: MemoryObservation,
    *,
    decision: MemoryAdmissionDecision | None = None,
) -> MemoryObservationSnapshot:
    return MemoryObservationSnapshot(
        observation_id=observation.id,
        task_id=observation.task_id,
        session_id=observation.session_id,
        repo_url=observation.repo_url,
        worker_type=observation.worker_type,
        source=observation.source,
        event_type=observation.event_type,
        observed_at=observation.observed_at,
        summary=observation.summary,
        content=observation.content,
        metadata_payload=dict(observation.metadata_payload or {}),
        privacy_stripped=observation.privacy_stripped,
        admission_status=observation.admission_status,
        admission_processed_at=observation.admission_processed_at,
        admission_error=observation.admission_error,
        decision_id=decision.id if decision is not None else None,
        proposal_id=decision.proposal_id if decision is not None else None,
        durable_memory_id=decision.durable_memory_id if decision is not None else None,
        created_at=observation.created_at,
        updated_at=observation.updated_at,
    )


def _decision_repo_url(
    decision: MemoryAdmissionDecision,
    *,
    observation: MemoryObservation | None = None,
    task: Task | None = None,
) -> str | None:
    candidate_payload = (
        decision.candidate_payload if isinstance(decision.candidate_payload, dict) else {}
    )
    repo_url = candidate_payload.get("repo_url")
    if isinstance(repo_url, str) and repo_url.strip():
        return repo_url.strip()

    if (
        observation is not None
        and isinstance(observation.repo_url, str)
        and observation.repo_url.strip()
    ):
        return observation.repo_url.strip()

    if task is not None and isinstance(task.repo_url, str) and task.repo_url.strip():
        return task.repo_url.strip()

    return None


def _map_memory_admission_decision_to_snapshot(
    decision: MemoryAdmissionDecision,
    *,
    repo_url: str | None,
) -> MemoryAdmissionDecisionSnapshot:
    return MemoryAdmissionDecisionSnapshot(
        decision_id=decision.id,
        category=decision.category,
        memory_key=decision.memory_key,
        candidate_payload=dict(decision.candidate_payload or {}),
        decision=decision.decision,
        risk_level=decision.risk_level,
        reason=decision.reason,
        task_id=decision.task_id,
        session_id=decision.session_id,
        repo_url=repo_url,
        durable_memory_id=decision.durable_memory_id,
        proposal_id=decision.proposal_id,
        source_observation_id=decision.source_observation_id,
        created_at=decision.created_at,
        updated_at=decision.updated_at,
    )


def _map_project_memory_to_snapshot(
    memory: ProjectMemory,
    *,
    headline: str | None = None,
) -> ProjectMemorySnapshot:
    return ProjectMemorySnapshot(
        memory_id=memory.id,
        repo_url=memory.repo_url,
        memory_key=memory.memory_key,
        value=dict(memory.value or {}),
        headline=headline,
        source=memory.source,
        confidence=memory.confidence,
        scope=memory.scope,
        last_verified_at=memory.last_verified_at,
        requires_verification=memory.requires_verification,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


def get_operational_metrics(self: Any, window_hours: int | None = 24) -> OperationalMetrics:
    """Return aggregated operational metrics across tasks and runs."""
    since = utc_now() - timedelta(hours=window_hours) if window_hours else None
    with session_scope(self.session_factory) as session:
        task_repo = TaskRepository(session)
        run_repo = WorkerRunRepository(session)
        task_metrics = task_repo.get_metrics(since=since)
        run_metrics = run_repo.get_metrics(since=since)
        return OperationalMetrics(
            total_tasks=task_metrics["total_tasks"],
            retried_tasks=task_metrics["retried_tasks"],
            retry_rate=task_metrics["retry_rate"],
            status_counts=task_metrics["status_counts"],
            worker_usage=run_metrics["worker_usage"],
            runtime_mode_usage=run_metrics["runtime_mode_usage"],
            legacy_tool_loop_usage=run_metrics["legacy_tool_loop_usage"],
            avg_duration_seconds=run_metrics["avg_duration_seconds"],
            success_rate=run_metrics["success_rate"],
        )


def is_secret_encryption_active(self: Any) -> bool:
    """Return True if secret encryption is active."""
    return Task.is_secret_encryption_active()


def _task_summary(task_snapshot: TaskSnapshot) -> str | None:
    """Return the latest human-readable outcome summary for notifications."""
    if task_snapshot.latest_run is not None and task_snapshot.latest_run.summary is not None:
        return task_snapshot.latest_run.summary
    return None


def _log_task_outcome(self: Any, task_snapshot: TaskSnapshot) -> None:
    """Emit the structured task-run log required for execution-path tracing."""
    latest_run = task_snapshot.latest_run
    logger.info(
        "Persisted execution-path task run",
        extra={
            "session_id": task_snapshot.session_id,
            "task_id": task_snapshot.task_id,
            "chosen_worker": task_snapshot.chosen_worker,
            "route_reason": task_snapshot.route_reason,
            "workspace_id": latest_run.workspace_id if latest_run is not None else None,
            "start_timestamp": latest_run.started_at.isoformat() if latest_run else None,
            "end_timestamp": latest_run.finished_at.isoformat()
            if latest_run and latest_run.finished_at is not None
            else None,
            "final_status": task_snapshot.status,
            "changed_files_count": latest_run.files_changed_count if latest_run else 0,
            "artifact_list": [
                {
                    "name": artifact.name,
                    "uri": artifact.uri,
                    "artifact_type": artifact.artifact_type,
                }
                for artifact in (latest_run.artifacts if latest_run is not None else [])
            ],
        },
    )
