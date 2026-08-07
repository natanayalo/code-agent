"""Unit tests for orchestrator/execution_resume_service.py."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from db.enums import ExecutionPlanNodeStatus
from orchestrator import execution_resume_service as ers


@dataclass
class DummyPlanNode:
    node_id: str
    goal: str = "Test Goal"
    depends_on: list[str] | None = None
    task_spec: dict[str, Any] | None = None
    node_kind: str | None = "mutate"
    aggregation_role: str | None = None
    execution_mode: str | None = None
    parallel_safe: bool = False
    status: ExecutionPlanNodeStatus = ExecutionPlanNodeStatus.PENDING
    result_summary: str | None = None
    failure_kind: str | None = None
    changed_files: list[str] | None = None
    verification_outcome: dict[str, Any] | None = None
    output_artifacts: list[dict[str, Any]] | None = None
    retry_count: int = 0
    plan_id: str = "plan-1"


@dataclass
class DummyExecutionPlan:
    id: str = "plan-1"
    nodes: list[DummyPlanNode] | None = None


def test_restore_decomposed_execution_state_none_or_empty():
    assert ers.restore_decomposed_execution_state(None) == (None, [])
    assert ers.restore_decomposed_execution_state(DummyExecutionPlan(nodes=[])) == (None, [])


def test_restore_decomposed_execution_state_invalid_contract():
    node_bad_spec = DummyPlanNode(node_id="n1", task_spec=None, node_kind="inspect")
    plan = DummyExecutionPlan(nodes=[node_bad_spec])
    assert ers.restore_decomposed_execution_state(plan) == (None, [])

    node_no_kind = DummyPlanNode(node_id="n1", task_spec={"task_text": "text"}, node_kind=None)
    plan2 = DummyExecutionPlan(nodes=[node_no_kind])
    assert ers.restore_decomposed_execution_state(plan2) == (None, [])


def test_restore_decomposed_execution_state_invalid_validation():
    # task_spec missing required task_text in node payload causes
    # DecomposedTaskPlan validation to fail

    node_invalid = DummyPlanNode(node_id="n1", task_spec={}, node_kind="inspect")
    plan = DummyExecutionPlan(nodes=[node_invalid])
    assert ers.restore_decomposed_execution_state(plan) == (None, [])


def test_restore_decomposed_execution_state_happy_path():
    n1 = DummyPlanNode(
        node_id="n1",
        goal="Inspect repo",
        depends_on=[],
        task_spec={"goal": "inspect"},
        node_kind="inspect",
        status=ExecutionPlanNodeStatus.COMPLETED,
        result_summary="Inspection complete",
        changed_files=["README.md"],
    )
    n2 = DummyPlanNode(
        node_id="n2",
        goal="Verify code",
        depends_on=["n1"],
        task_spec={"goal": "verify"},
        node_kind="verify",
        status=ExecutionPlanNodeStatus.FAILED,
        result_summary="Verification failed",
        failure_kind="test_regression",
        retry_count=1,
    )
    n3 = DummyPlanNode(
        node_id="n3",
        goal="Pending step",
        depends_on=["n2"],
        task_spec={"goal": "step 3"},
        node_kind="implement",
        status=ExecutionPlanNodeStatus.PENDING,
    )
    plan = DummyExecutionPlan(nodes=[n1, n2, n3])

    plan_dict, outcomes = ers.restore_decomposed_execution_state(plan)
    assert plan_dict is not None
    assert plan_dict["triggered"] is True
    assert plan_dict["reason"] == "restored_execution_plan"
    assert len(plan_dict["nodes"]) == 3

    assert len(outcomes) == 2
    assert outcomes[0]["node_id"] == "n1"
    assert outcomes[0]["result"]["status"] == "success"
    assert outcomes[1]["node_id"] == "n2"
    assert outcomes[1]["result"]["status"] == "failure"
    assert outcomes[1]["attempts"] == 2


def test_aggregation_role():
    assert ers._aggregation_role("inspect") == "context"
    assert ers._aggregation_role("verify") == "validation"
    assert ers._aggregation_role("implement") == "mutation"
    assert ers._aggregation_role("other") == "mutation"


def test_restored_node_outcome_validation_error():
    # Pass node with invalid status type/value to trigger ValidationError inside model_validate
    class BadStatus:
        value = "invalid_status_value"

    bad_node = DummyPlanNode(
        node_id="n1",
        status=ExecutionPlanNodeStatus.COMPLETED,
    )
    bad_node.status = BadStatus()  # type: ignore
    res = ers._restored_node_outcome(bad_node)
    assert res is None
