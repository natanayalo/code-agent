"""Unit tests for orchestrator/execution_resume_service.py."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from db.enums import ExecutionPlanNodeStatus
from orchestrator import execution_resume_service as ers
from orchestrator.state import NodeOutcome
from workers import WorkerResult


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
    latest_logical_activity_key: str | None = None
    merged_logical_activity_key: str | None = None
    terminal_result_digest: str | None = None
    terminal_result_payload: dict[str, Any] | None = None


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
    res1 = WorkerResult(
        status="success", summary="Inspection complete", files_changed=["README.md"]
    )
    out1 = NodeOutcome(
        node_id="n1", status="completed", result=res1, attempts=1, logical_activity_key="k1"
    )
    p1 = {
        "worker_result": res1.model_dump(mode="json"),
        "node_outcome": out1.model_dump(mode="json"),
    }
    n1 = DummyPlanNode(
        node_id="n1",
        goal="Inspect repo",
        depends_on=[],
        task_spec={"goal": "inspect"},
        node_kind="inspect",
        status=ExecutionPlanNodeStatus.COMPLETED,
        latest_logical_activity_key="k1",
        merged_logical_activity_key="k1",
        terminal_result_payload=p1,
    )

    res2 = WorkerResult(
        status="failure", summary="Verification failed", failure_kind="test_regression"
    )
    out2 = NodeOutcome(
        node_id="n2", status="failed", result=res2, attempts=2, logical_activity_key="k2"
    )
    p2 = {
        "worker_result": res2.model_dump(mode="json"),
        "node_outcome": out2.model_dump(mode="json"),
    }
    n2 = DummyPlanNode(
        node_id="n2",
        goal="Verify code",
        depends_on=["n1"],
        task_spec={"goal": "verify"},
        node_kind="verify",
        status=ExecutionPlanNodeStatus.FAILED,
        latest_logical_activity_key="k2",
        merged_logical_activity_key="k2",
        terminal_result_payload=p2,
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


def test_restore_merged_node_outcomes_fails_closed_on_missing_payload():
    node = DummyPlanNode(
        node_id="n1",
        status=ExecutionPlanNodeStatus.COMPLETED,
        merged_logical_activity_key="k1",
        latest_logical_activity_key="k1",
        terminal_result_payload=None,
    )
    plan = DummyExecutionPlan(nodes=[node])
    with pytest.raises(RuntimeError, match="Cannot reconstruct merged outcome"):
        ers.restore_merged_node_outcomes(plan)
