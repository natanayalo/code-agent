"""Unit tests for orchestrator/execution_outcome_service.py helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from orchestrator.execution_outcome_service import (
    _apply_approval_constraints,
    _build_artifact_index,
    _observation_bridge_has_activity,
    _should_create_scout_proposal,
)
from orchestrator.state import ApprovalCheckpoint, OrchestratorState
from workers import ArtifactReference, WorkerResult


def test_observation_bridge_has_activity():
    assert _observation_bridge_has_activity({}) is False
    assert _observation_bridge_has_activity({"extracted_candidate_count": 1}) is True
    assert _observation_bridge_has_activity({"proposal_count": 2}) is True
    assert _observation_bridge_has_activity({"durable_memory_count": 1}) is True
    assert _observation_bridge_has_activity({"decision_counts": {"admitted": 1}}) is True


def test_apply_approval_constraints():
    task = MagicMock()
    task.constraints = {}
    state_no_req = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    _apply_approval_constraints(task, state_no_req, datetime.now(UTC))
    assert "approval" not in task.constraints

    state_app = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        approval=ApprovalCheckpoint(
            required=True, status="approved", approval_type="permission", reason="Approved by user"
        ),
    )
    _apply_approval_constraints(task, state_app, datetime.now(UTC))
    assert task.constraints["approval"]["approved"] is True


def test_build_artifact_index():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="success", summary="done"),
    )
    artifacts = [ArtifactReference(name="a1", uri="file:///a1", artifact_type="log")]
    index, review_entries = _build_artifact_index(state, artifacts)
    assert len(index) >= 1
    assert index[0]["name"] == "a1"


def test_should_create_scout_proposal():
    state_not_scout = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="success"),
    )
    assert _should_create_scout_proposal(state_not_scout) is False


def test_update_task_route_and_spec():
    from orchestrator.execution_outcome_service import _update_task_route_and_spec
    from orchestrator.state import RouteDecision, TaskPlan, TaskPlanStep, TaskSpec

    task = MagicMock()
    task.id = "t1"
    task.constraints = {}
    task.execution_plan = None

    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        route=RouteDecision(chosen_worker="codex", route_reason="tested"),
        task_spec=TaskSpec(goal="G", acceptance_criteria=["AC"], task_type="feature"),
        task_plan=TaskPlan(
            steps=[TaskPlanStep(step_id="step-1", title="Step 1", expected_outcome="done")]
        ),
    )

    interaction_repo = MagicMock()
    plan_repo = MagicMock()
    plan_node = MagicMock()
    plan_repo.create.return_value = MagicMock(nodes=[])
    plan_repo.add_node.return_value = plan_node

    _update_task_route_and_spec(task, state, interaction_repo, plan_repo)
    assert task.chosen_worker == "codex"
    plan_repo.add_node.assert_called_once()
