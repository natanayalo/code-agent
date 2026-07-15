"""Restore durable decomposed-execution state for queued task retries."""

from __future__ import annotations

import logging
from typing import Any

from db.enums import ExecutionPlanNodeStatus
from orchestrator.state import DecomposedTaskPlan, NodeOutcome

logger = logging.getLogger("orchestrator.execution")


def restore_decomposed_execution_state(
    execution_plan: Any,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Rebuild durable DAG state from persisted execution-plan nodes."""
    if execution_plan is None or not execution_plan.nodes:
        return None, []

    node_payloads: list[dict[str, Any]] = []
    for node in execution_plan.nodes:
        if not isinstance(node.task_spec, dict) or not node.node_kind:
            logger.warning(
                "Skipping persisted DAG restore because node contract is incomplete.",
                extra={"plan_id": execution_plan.id, "node_id": node.node_id},
            )
            return None, []
        node_payloads.append(_decomposed_node_payload(node))
    try:
        decomposed_plan = DecomposedTaskPlan.model_validate(
            {
                "triggered": True,
                "status": "decomposed",
                "reason": "restored_execution_plan",
                "nodes": node_payloads,
            }
        )
    except ValueError:
        logger.warning(
            "Skipping persisted DAG restore because its node contracts are invalid.",
            extra={"plan_id": execution_plan.id},
            exc_info=True,
        )
        return None, []

    outcomes = [
        outcome.model_dump(mode="json")
        for node in execution_plan.nodes
        if (outcome := _restored_node_outcome(node)) is not None
    ]
    return decomposed_plan.model_dump(mode="json"), outcomes


def _decomposed_node_payload(node: Any) -> dict[str, Any]:
    dependencies = list(node.depends_on or [])
    return {
        "node_id": node.node_id,
        "title": node.goal,
        "depends_on": dependencies,
        "task_spec": node.task_spec,
        "node_kind": node.node_kind,
        "expected_inputs": ["parent_task_context", *dependencies],
        "expected_outputs": ["summary", "validation_evidence"],
        "aggregation_role": getattr(node, "aggregation_role", _aggregation_role(node.node_kind)),
        "execution_mode": getattr(node, "execution_mode", "mutable"),
        "parallel_safe": bool(getattr(node, "parallel_safe", False)),
    }


def _aggregation_role(node_kind: str) -> str:
    if node_kind == "inspect":
        return "context"
    if node_kind == "verify":
        return "validation"
    return "mutation"


def _restored_node_outcome(node: Any) -> NodeOutcome | None:
    if node.status not in _TERMINAL_NODE_STATUSES:
        return None
    try:
        return NodeOutcome.model_validate(
            {
                "node_id": node.node_id,
                "status": node.status.value,
                "result": {
                    "status": (
                        "success" if node.status is ExecutionPlanNodeStatus.COMPLETED else "failure"
                    ),
                    "summary": node.result_summary,
                    "failure_kind": node.failure_kind,
                    "commands_run": [],
                    "files_changed": list(node.changed_files or []),
                    "test_results": (node.verification_outcome or {}).get("test_results", []),
                    "artifacts": list(node.output_artifacts or []),
                },
                "dependencies": list(node.depends_on or []),
                "attempts": max(node.retry_count + 1, 1),
            }
        )
    except ValueError:
        logger.warning(
            "Skipping invalid persisted node outcome during DAG restore.",
            extra={"plan_id": node.plan_id, "node_id": node.node_id},
            exc_info=True,
        )
        return None


_TERMINAL_NODE_STATUSES = frozenset(
    {
        ExecutionPlanNodeStatus.BLOCKED,
        ExecutionPlanNodeStatus.COMPLETED,
        ExecutionPlanNodeStatus.FAILED,
        ExecutionPlanNodeStatus.SKIPPED,
    }
)
