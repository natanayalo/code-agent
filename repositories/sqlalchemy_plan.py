"""Execution plan SQLAlchemy repositories."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from db.base import generate_uuid
from db.enums import ExecutionPlanNodeStatus
from db.models import ExecutionPlan, ExecutionPlanNode


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
        worker_run_id: str | None | Any = ...,
        result_summary: str | None | Any = ...,
        failure_kind: str | None | Any = ...,
        verification_outcome: dict[str, Any] | None | Any = ...,
        changed_files: list[str] | None | Any = ...,
        output_artifacts: list[dict[str, Any]] | None | Any = ...,
        last_attempt_at: datetime | None | Any = ...,
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

        return node
