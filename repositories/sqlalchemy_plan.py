"""Execution plan SQLAlchemy repositories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import set_committed_value

from db.base import generate_uuid, utc_now
from db.enums import ExecutionPlanNodeStatus
from db.models import ExecutionPlan, ExecutionPlanNode, ExecutionPlanNodeAttempt


class ExecutionPlanRepository:
    """Persist and query task execution plans and their nodes."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, *, task_id: str) -> ExecutionPlan:
        """Create a new execution plan for a task."""
        plan = ExecutionPlan(id=generate_uuid(), task_id=task_id)
        self.session.add(plan)
        return plan

    def get_by_task_id(self, task_id: str) -> ExecutionPlan | None:
        """Retrieve an execution plan and its nodes for a given task ID."""
        stmt = (
            select(ExecutionPlan)
            .where(ExecutionPlan.task_id == task_id)
            .options(selectinload(ExecutionPlan.nodes))
        )
        return self.session.scalars(stmt).first()

    def get_by_id(self, plan_id: str) -> ExecutionPlan | None:
        """Retrieve an execution plan by its ID."""
        return self.session.get(ExecutionPlan, plan_id)

    def add_node(
        self,
        *,
        plan_id: str,
        node_id: str,
        goal: str,
        sequence_number: int = 0,
        status: ExecutionPlanNodeStatus = ExecutionPlanNodeStatus.PENDING,
        depends_on: list[str] | None = None,
        task_spec: dict[str, Any] | None = None,
        node_kind: str | None = None,
        aggregation_role: str = "mutation",
        execution_mode: str = "mutable",
        parallel_safe: bool = False,
        acceptance_criteria: str | None = None,
        assigned_worker_profile: str | None = None,
        budget: dict[str, Any] | None = None,
        validation_commands: list[str] | None = None,
        artifacts: list[str] | None = None,
    ) -> ExecutionPlanNode:
        """Add a new node to an existing execution plan."""
        node = ExecutionPlanNode(
            id=generate_uuid(),
            plan_id=plan_id,
            node_id=node_id,
            sequence_number=sequence_number,
            depends_on=depends_on,
            task_spec=task_spec,
            node_kind=node_kind,
            aggregation_role=aggregation_role,
            execution_mode=execution_mode,
            parallel_safe=parallel_safe,
            status=status,
            goal=goal,
            acceptance_criteria=acceptance_criteria,
            assigned_worker_profile=assigned_worker_profile,
            budget=budget,
            validation_commands=validation_commands,
            artifacts=artifacts,
        )
        self.session.add(node)
        return node

    def get_node(self, plan_id: str, node_id: str) -> ExecutionPlanNode | None:
        """Retrieve a specific node from an execution plan."""
        stmt = select(ExecutionPlanNode).where(
            ExecutionPlanNode.plan_id == plan_id, ExecutionPlanNode.node_id == node_id
        )
        return self.session.scalars(stmt).first()

    def update_node(
        self,
        *,
        plan_id: str,
        node_id: str,
        status: ExecutionPlanNodeStatus | Any = ...,
        assigned_worker_profile: str | None | Any = ...,
        budget: dict[str, Any] | None | Any = ...,
        validation_commands: list[str] | None | Any = ...,
        artifacts: list[str] | None | Any = ...,
        blocker_interaction_id: str | None | Any = ...,
        retry_count: int | Any = ...,
        started_at: datetime | None | Any = ...,
        finished_at: datetime | None | Any = ...,
        task_spec: dict[str, Any] | None | Any = ...,
        node_kind: str | None | Any = ...,
        aggregation_role: str | Any = ...,
        execution_mode: str | Any = ...,
        parallel_safe: bool | Any = ...,
        worker_run_id: str | None | Any = ...,
        result_summary: str | None | Any = ...,
        failure_kind: str | None | Any = ...,
        verification_outcome: dict[str, Any] | None | Any = ...,
        changed_files: list[str] | None | Any = ...,
        output_artifacts: list[dict[str, Any]] | None | Any = ...,
        last_attempt_at: datetime | None | Any = ...,
        latest_logical_activity_key: str | None | Any = ...,
        terminal_result_schema_version: int | None | Any = ...,
        terminal_result_digest: str | None | Any = ...,
        terminal_result_payload: dict[str, Any] | None | Any = ...,
    ) -> ExecutionPlanNode | None:
        """Update fields of an execution plan node."""
        node = self.get_node(plan_id, node_id)
        if not node:
            return None

        if status is not ...:
            node.status = status
        if assigned_worker_profile is not ...:
            node.assigned_worker_profile = assigned_worker_profile
        if budget is not ...:
            node.budget = budget
        if validation_commands is not ...:
            node.validation_commands = validation_commands
        if artifacts is not ...:
            node.artifacts = artifacts
        if blocker_interaction_id is not ...:
            node.blocker_interaction_id = blocker_interaction_id
        if retry_count is not ...:
            node.retry_count = retry_count
        if started_at is not ...:
            node.started_at = started_at
        if finished_at is not ...:
            node.finished_at = finished_at
        if task_spec is not ...:
            node.task_spec = task_spec
        if node_kind is not ...:
            node.node_kind = node_kind
        if aggregation_role is not ...:
            node.aggregation_role = aggregation_role
        if execution_mode is not ...:
            node.execution_mode = execution_mode
        if parallel_safe is not ...:
            node.parallel_safe = parallel_safe
        if worker_run_id is not ...:
            node.worker_run_id = worker_run_id
        if result_summary is not ...:
            node.result_summary = result_summary
        if failure_kind is not ...:
            node.failure_kind = failure_kind
        if verification_outcome is not ...:
            node.verification_outcome = verification_outcome
        if changed_files is not ...:
            node.changed_files = changed_files
        if output_artifacts is not ...:
            node.output_artifacts = output_artifacts
        if last_attempt_at is not ...:
            node.last_attempt_at = last_attempt_at
        if latest_logical_activity_key is not ...:
            node.latest_logical_activity_key = latest_logical_activity_key
        if terminal_result_schema_version is not ...:
            node.terminal_result_schema_version = terminal_result_schema_version
        if terminal_result_digest is not ...:
            node.terminal_result_digest = terminal_result_digest
        if terminal_result_payload is not ...:
            node.terminal_result_payload = terminal_result_payload

        return node

    def start_attempt(
        self,
        *,
        plan_id: str,
        node_id: str,
        effective_input_summary: dict[str, Any],
        effective_input_digest: str,
        worker_type: str | None,
        worker_profile: str | None,
        runtime_mode: str | None,
        workspace_id: str | None,
        task_trace_id: str | None,
        worker_run_id: str | None = None,
        logical_activity_key: str | None = None,
        claim_token: str | None = None,
        claim_expires_at: datetime | None = None,
        max_retries: int = 5,
    ) -> ExecutionPlanNodeAttempt:
        """Persist an attempt before a worker is dispatched."""
        node = self.get_node(plan_id, node_id)
        if node is None:
            raise ValueError(f"Unknown execution plan node: {node_id}")
        for attempt_index in range(max_retries):
            try:
                with self.session.begin_nested():
                    existing_attempt_count = (
                        self.session.scalar(
                            select(
                                func.coalesce(func.max(ExecutionPlanNodeAttempt.attempt_number), 0)
                            ).where(ExecutionPlanNodeAttempt.plan_node_id == node.id)
                        )
                        or 0
                    )
                    attempt_number = existing_attempt_count + 1
                    attempt = ExecutionPlanNodeAttempt(
                        id=generate_uuid(),
                        plan_node_id=node.id,
                        attempt_number=attempt_number,
                        started_at=utc_now(),
                        effective_input_summary=effective_input_summary,
                        effective_input_digest=effective_input_digest,
                        worker_type=worker_type,
                        worker_profile=worker_profile,
                        runtime_mode=runtime_mode,
                        workspace_id=workspace_id,
                        worker_run_id=worker_run_id,
                        task_trace_id=task_trace_id,
                        logical_activity_key=logical_activity_key,
                        claim_token=claim_token,
                        heartbeat_at=utc_now() if claim_token else None,
                        claim_expires_at=claim_expires_at,
                    )
                    self.session.add(attempt)
                    self.session.flush()
                    return attempt
            except IntegrityError:
                if attempt_index == max_retries - 1:
                    raise
        raise RuntimeError("Unable to allocate execution-plan attempt number.")

    def claim_activity(
        self,
        *,
        plan_id: str,
        node_id: str,
        logical_activity_key: str,
        effective_input_summary: dict[str, Any],
        effective_input_digest: str,
        worker_type: str | None,
        worker_profile: str | None,
        runtime_mode: str | None,
        workspace_id: str | None,
        task_trace_id: str | None,
        lease_seconds: int = 30,
    ) -> tuple[str, ExecutionPlanNodeAttempt]:
        """Atomically claim, replay, or reject one logical node activity."""
        node = self.get_node(plan_id, node_id)
        if node is None:
            raise ValueError(f"Unknown execution plan node: {node_id}")
        existing = self.session.scalar(
            select(ExecutionPlanNodeAttempt).where(
                ExecutionPlanNodeAttempt.plan_node_id == node.id,
                ExecutionPlanNodeAttempt.logical_activity_key == logical_activity_key,
            )
        )
        if existing is not None:
            if existing.effective_input_digest != effective_input_digest:
                return "collision", existing
            if existing.result_payload is not None:
                return "terminal_replay", existing
            now = utc_now()
            token = uuid4().hex
            expiry = now + timedelta(seconds=lease_seconds)
            claimed = self.session.execute(
                update(ExecutionPlanNodeAttempt)
                .where(
                    ExecutionPlanNodeAttempt.id == existing.id,
                    ExecutionPlanNodeAttempt.claim_generation == existing.claim_generation,
                    ExecutionPlanNodeAttempt.result_payload.is_(None),
                    or_(
                        ExecutionPlanNodeAttempt.claim_expires_at.is_(None),
                        ExecutionPlanNodeAttempt.claim_expires_at <= now,
                    ),
                )
                .values(
                    claim_generation=ExecutionPlanNodeAttempt.claim_generation + 1,
                    claim_token=token,
                    heartbeat_at=now,
                    claim_expires_at=expiry,
                )
            )
            if cast(Any, claimed).rowcount == 1:
                self.session.refresh(existing)
                return "new", existing
            self.session.expire(existing)
            refreshed = self.session.get(ExecutionPlanNodeAttempt, existing.id)
            if refreshed is None:
                raise RuntimeError("claimed node activity disappeared")
            return "in_progress", refreshed
        token = uuid4().hex
        try:
            attempt = self.start_attempt(
                plan_id=plan_id,
                node_id=node_id,
                effective_input_summary=effective_input_summary,
                effective_input_digest=effective_input_digest,
                worker_type=worker_type,
                worker_profile=worker_profile,
                runtime_mode=runtime_mode,
                workspace_id=workspace_id,
                task_trace_id=task_trace_id,
                logical_activity_key=logical_activity_key,
                claim_token=token,
                claim_expires_at=utc_now() + timedelta(seconds=lease_seconds),
            )
        except IntegrityError:
            return self.claim_activity(
                plan_id=plan_id,
                node_id=node_id,
                logical_activity_key=logical_activity_key,
                effective_input_summary=effective_input_summary,
                effective_input_digest=effective_input_digest,
                worker_type=worker_type,
                worker_profile=worker_profile,
                runtime_mode=runtime_mode,
                workspace_id=workspace_id,
                task_trace_id=task_trace_id,
                lease_seconds=lease_seconds,
            )
        return "new", attempt

    def heartbeat_activity(
        self, *, attempt_id: str, claim_token: str, lease_seconds: int = 30
    ) -> bool:
        """Extend a claim only when the caller still owns it."""
        now = utc_now()
        updated = self.session.execute(
            update(ExecutionPlanNodeAttempt)
            .where(
                ExecutionPlanNodeAttempt.id == attempt_id,
                ExecutionPlanNodeAttempt.claim_token == claim_token,
                ExecutionPlanNodeAttempt.result_payload.is_(None),
            )
            .values(
                heartbeat_at=now,
                claim_expires_at=now + timedelta(seconds=lease_seconds),
            )
        )
        return cast(Any, updated).rowcount == 1

    def finish_attempt(
        self,
        *,
        attempt_id: str,
        status: str,
        failure_kind: str | None,
        workspace_id: str | None = None,
        claim_token: str | None = None,
        result_payload: dict[str, Any] | None = None,
        result_schema_version: int | None = None,
        result_digest: str | None = None,
    ) -> ExecutionPlanNodeAttempt | None:
        """Finalize a started attempt without modifying historical attempts."""
        attempt = self.session.get(ExecutionPlanNodeAttempt, attempt_id)
        if attempt is None:
            return None
        finished_at = utc_now()
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=UTC)
        started_at = attempt.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        conditions = [
            ExecutionPlanNodeAttempt.id == attempt_id,
            ExecutionPlanNodeAttempt.result_payload.is_(None),
        ]
        if claim_token is not None:
            conditions.append(ExecutionPlanNodeAttempt.claim_token == claim_token)
        values: dict[str, Any] = {
            "finished_at": finished_at,
            "duration_ms": max(0, int((finished_at - started_at).total_seconds() * 1000)),
            "status": status,
            "failure_kind": failure_kind,
            "result_payload": result_payload,
            "result_schema_version": result_schema_version,
            "result_digest": result_digest,
            "claim_expires_at": finished_at,
        }
        if workspace_id is not None:
            values["workspace_id"] = workspace_id
        updated = self.session.execute(
            update(ExecutionPlanNodeAttempt).where(*conditions).values(**values)
        )
        if cast(Any, updated).rowcount != 1:
            return None
        for attribute, value in values.items():
            set_committed_value(attempt, attribute, value)
        return attempt
