"""Unit tests for Temporal interaction primitives on TaskExecutionWorkflow (M30)."""

from __future__ import annotations

import pytest

from orchestrator.temporal.workflows import TaskExecutionWorkflow


def test_workflow_initial_queries():
    workflow = TaskExecutionWorkflow()
    status = workflow.get_status()
    assert status == {
        "task_id": None,
        "lifecycle_stage": "initializing",
        "escalation_count": 0,
        "completion_continuation": None,
        "has_pending_decision": False,
    }
    assert workflow.get_current_plan() == {}
    assert workflow.get_pending_decision() is None


def test_workflow_validate_operator_decision_invalid_kind():
    workflow = TaskExecutionWorkflow()
    with pytest.raises(ValueError, match="Invalid decision_kind 'unknown'"):
        workflow.validate_operator_decision({"decision_kind": "unknown"})


def test_workflow_validate_operator_decision_no_pending():
    workflow = TaskExecutionWorkflow()
    assert workflow.pending_decision is None
    with pytest.raises(ValueError, match="Workflow is not awaiting 'approval'"):
        workflow.validate_operator_decision({"decision_kind": "approval", "value": True})


def test_workflow_validate_operator_decision_mismatched_kind():
    workflow = TaskExecutionWorkflow()
    workflow.pending_decision = {"kind": "clarification", "node_id": None, "escalation_count": 0}
    with pytest.raises(ValueError, match="Workflow is not awaiting 'approval'"):
        workflow.validate_operator_decision({"decision_kind": "approval", "value": True})


def test_workflow_validate_operator_decision_idempotent_duplicate_key():
    workflow = TaskExecutionWorkflow()
    workflow.processed_command_keys.add("cmd-123")
    # Even if not awaiting approval, a previously processed command_key is valid
    workflow.validate_operator_decision({"decision_kind": "approval", "command_key": "cmd-123"})


def test_workflow_validate_operator_decision_requires_boolean_value():
    workflow = TaskExecutionWorkflow()
    workflow.pending_decision = {"kind": "approval", "node_id": None, "escalation_count": 0}
    with pytest.raises(ValueError, match="requires a boolean 'value' field"):
        workflow.validate_operator_decision({"decision_kind": "approval"})

    with pytest.raises(ValueError, match="requires a boolean 'value' field"):
        workflow.validate_operator_decision({"decision_kind": "approval", "value": "yes"})

    workflow.pending_decision = {
        "kind": "permission_escalation",
        "node_id": None,
        "escalation_count": 0,
    }
    with pytest.raises(ValueError, match="requires a boolean 'value' field"):
        workflow.validate_operator_decision({"decision_kind": "permission_escalation"})


def test_workflow_apply_operator_decision_approval():
    workflow = TaskExecutionWorkflow()
    workflow.pending_decision = {"kind": "approval", "node_id": None, "escalation_count": 0}
    workflow.validate_operator_decision(
        {"decision_kind": "approval", "value": True, "command_key": "cmd-approve"}
    )
    result = workflow.apply_operator_decision(
        {"decision_kind": "approval", "value": True, "command_key": "cmd-approve"}
    )
    assert result == {
        "applied": True,
        "duplicate": False,
        "decision_kind": "approval",
        "lifecycle_stage": "initializing",
    }
    assert workflow.approval_decision is True
    assert workflow.pending_decision is None
    assert "cmd-approve" in workflow.processed_command_keys

    # Duplicate call with same command_key is accepted by validator and recognized as dupe
    workflow.validate_operator_decision(
        {"decision_kind": "approval", "value": False, "command_key": "cmd-approve"}
    )
    dupe_result = workflow.apply_operator_decision(
        {"decision_kind": "approval", "value": False, "command_key": "cmd-approve"}
    )
    assert dupe_result["applied"] is False
    assert dupe_result["duplicate"] is True
    # State was not overwritten
    assert workflow.approval_decision is True

    # But a different command_key is rejected because pending_decision was cleared
    with pytest.raises(ValueError, match="Workflow is not awaiting 'approval'"):
        workflow.validate_operator_decision(
            {"decision_kind": "approval", "value": True, "command_key": "cmd-other"}
        )


@pytest.mark.anyio
async def test_workflow_signals_clear_pending_decision():
    workflow = TaskExecutionWorkflow()
    workflow.pending_decision = {"kind": "approval", "node_id": None, "escalation_count": 0}
    await workflow.handle_approval(True)
    assert workflow.approval_decision is True
    assert workflow.pending_decision is None

    workflow.pending_decision = {"kind": "clarification", "node_id": None, "escalation_count": 0}
    await workflow.handle_clarification({"answer": "ok"})
    assert workflow.clarification_resolved is True
    assert workflow.pending_decision is None

    workflow.pending_decision = {
        "kind": "permission_escalation",
        "node_id": None,
        "escalation_count": 1,
    }
    await workflow.handle_permission_escalation(True)
    assert workflow.permission_escalation_decision is True
    assert workflow.pending_decision is None


def test_workflow_apply_operator_decision_clarification():
    workflow = TaskExecutionWorkflow()
    workflow.pending_decision = {"kind": "clarification", "node_id": None, "escalation_count": 0}
    result = workflow.apply_operator_decision(
        {"decision_kind": "clarification", "command_key": "cmd-clarify"}
    )
    assert result["applied"] is True
    assert workflow.clarification_resolved is True


def test_workflow_apply_operator_decision_permission_escalation():
    workflow = TaskExecutionWorkflow()
    workflow.pending_decision = {
        "kind": "permission_escalation",
        "node_id": "node-1",
        "escalation_count": 1,
    }
    result = workflow.apply_operator_decision(
        {"decision_kind": "permission_escalation", "value": True, "command_key": "cmd-escalate"}
    )
    assert result["applied"] is True
    assert workflow.permission_escalation_decision is True


def test_workflow_queries_reflect_runtime_updates():
    workflow = TaskExecutionWorkflow()
    workflow.task_id = "task-42"
    workflow.lifecycle_stage = "awaiting_approval"
    workflow.escalation_count = 2
    workflow.completion_continuation = "repair"
    workflow.current_plan = {
        "execution_shape": "decomposed",
        "use_node_waves": True,
        "active_node_ids": ["node-a"],
    }
    workflow.pending_decision = {
        "kind": "approval",
        "node_id": None,
        "escalation_count": 2,
    }

    status = workflow.get_status()
    assert status == {
        "task_id": "task-42",
        "lifecycle_stage": "awaiting_approval",
        "escalation_count": 2,
        "completion_continuation": "repair",
        "has_pending_decision": True,
    }
    assert workflow.get_current_plan() == {
        "execution_shape": "decomposed",
        "use_node_waves": True,
        "active_node_ids": ["node-a"],
    }
    assert workflow.get_pending_decision() == {
        "kind": "approval",
        "node_id": None,
        "escalation_count": 2,
    }
