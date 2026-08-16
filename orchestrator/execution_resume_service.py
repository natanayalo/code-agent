"""Restore durable decomposed-execution state for queued task retries."""

from __future__ import annotations

import logging
from typing import Any

from db.enums import TimelineEventType
from orchestrator.state import DecomposedTaskPlan, NodeOutcome, TaskPlan, TaskSpec
from repositories.sqlalchemy_plan import ExecutionPlanRepository

logger = logging.getLogger("orchestrator.execution")


def restore_task_plan_from_events(timeline_events: list[Any]) -> TaskPlan | None:
    """Restore exact TaskPlan from authoritative TASK_PLANNED timeline events."""
    if not timeline_events:
        return None

    for event in reversed(timeline_events):
        event_type = (
            event.event_type.value
            if hasattr(event.event_type, "value")
            else str(getattr(event, "event_type", ""))
        )
        if event_type != TimelineEventType.TASK_PLANNED.value and event_type != "task_planned":
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            raise RuntimeError(
                "Malformed TASK_PLANNED timeline event payload: payload must be a dictionary."
            )
        # Skip decomposition events when searching for planning event
        if (
            "decomposition" in payload
            and payload.get("planning") != "generated"
            and "steps" not in payload
        ):
            continue

        if payload.get("planning") == "generated" or "steps" in payload:
            if not isinstance(payload.get("steps"), list):
                raise RuntimeError("Malformed TASK_PLANNED steps payload: 'steps' must be a list.")
            if "triggered" not in payload:
                raise RuntimeError(
                    "Malformed TASK_PLANNED steps payload: 'triggered' field is required."
                )
            try:
                return TaskPlan.model_validate(
                    {
                        "triggered": payload["triggered"],
                        "complexity_reason": payload.get("complexity_reason"),
                        "steps": payload["steps"],
                    }
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Malformed TASK_PLANNED steps payload in timeline event: {exc}"
                ) from exc

        raise RuntimeError(
            "Malformed TASK_PLANNED timeline event payload: missing planning or decomposition data."
        )
    return None


def restore_decomposed_plan_from_events(timeline_events: list[Any]) -> DecomposedTaskPlan | None:
    """Restore exact DecomposedTaskPlan from authoritative TASK_PLANNED timeline events."""
    if not timeline_events:
        return None

    for event in reversed(timeline_events):
        event_type = (
            event.event_type.value
            if hasattr(event.event_type, "value")
            else str(getattr(event, "event_type", ""))
        )
        if event_type != TimelineEventType.TASK_PLANNED.value and event_type != "task_planned":
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            raise RuntimeError(
                "Malformed TASK_PLANNED timeline event payload: payload must be a dictionary."
            )
        # Skip planning events when searching for decomposition event
        if ("planning" in payload or "steps" in payload) and "decomposition" not in payload:
            continue

        if "decomposition" in payload:
            decomp_payload = payload.get("decomposition")
            if not isinstance(decomp_payload, dict):
                raise RuntimeError(
                    "Malformed TASK_PLANNED decomposition payload: not a dictionary."
                )
            try:
                return DecomposedTaskPlan.model_validate(decomp_payload)
            except Exception as exc:
                raise RuntimeError(
                    f"Malformed TASK_PLANNED decomposition payload in timeline event: {exc}"
                ) from exc

        raise RuntimeError(
            "Malformed TASK_PLANNED timeline event payload: missing planning or decomposition data."
        )
    return None


def _validate_single_node_projection(
    sql_node: Any,
    model_node: Any,
    seq_idx: int,
) -> None:
    """Validate a single operational node against its restored DecomposedTaskNode model."""
    if sql_node.node_id != model_node.node_id:
        raise RuntimeError(
            f"ExecutionPlan node at index {seq_idx} has node_id '{sql_node.node_id}', "
            f"expected '{model_node.node_id}'."
        )
    expected_goals = {model_node.title}
    if model_node.task_spec and model_node.task_spec.goal:
        expected_goals.add(model_node.task_spec.goal)
    if sql_node.goal not in expected_goals:
        raise RuntimeError(
            f"ExecutionPlan node '{sql_node.node_id}' goal '{sql_node.goal}' "
            f"does not match title '{model_node.title}'."
        )
    sql_deps = list(sql_node.depends_on or [])
    model_deps = list(model_node.depends_on or [])
    if sql_deps != model_deps:
        raise RuntimeError(
            f"ExecutionPlan node '{sql_node.node_id}' depends_on {sql_deps} "
            f"does not match {model_deps}."
        )
    if sql_node.node_kind != model_node.node_kind:
        raise RuntimeError(
            f"ExecutionPlan node '{sql_node.node_id}' node_kind '{sql_node.node_kind}' "
            f"does not match '{model_node.node_kind}'."
        )
    if sql_node.aggregation_role != model_node.aggregation_role:
        raise RuntimeError(
            f"ExecutionPlan node '{sql_node.node_id}' aggregation_role "
            f"'{sql_node.aggregation_role}' does not match '{model_node.aggregation_role}'."
        )
    if sql_node.execution_mode != model_node.execution_mode:
        raise RuntimeError(
            f"ExecutionPlan node '{sql_node.node_id}' execution_mode "
            f"'{sql_node.execution_mode}' does not match '{model_node.execution_mode}'."
        )
    if bool(sql_node.parallel_safe) != bool(model_node.parallel_safe):
        raise RuntimeError(
            f"ExecutionPlan node '{sql_node.node_id}' parallel_safe '{sql_node.parallel_safe}' "
            f"does not match '{model_node.parallel_safe}'."
        )
    if isinstance(sql_node.task_spec, dict) and model_node.task_spec is not None:
        try:
            sql_spec = TaskSpec.model_validate(sql_node.task_spec)
            if sql_spec != model_node.task_spec:
                raise RuntimeError(
                    f"ExecutionPlan node '{sql_node.node_id}' task_spec "
                    "does not match restored model."
                )
        except Exception as exc:
            raise RuntimeError(
                f"ExecutionPlan node '{sql_node.node_id}' task_spec validation failed: {exc}"
            ) from exc
    elif (sql_node.task_spec is not None) != (model_node.task_spec is not None):
        raise RuntimeError(
            f"ExecutionPlan node '{sql_node.node_id}' task_spec presence "
            "does not match restored model."
        )


def validate_decomposed_plan_projection(
    execution_plan: Any,
    decomposed_plan: DecomposedTaskPlan,
) -> None:
    """Validate that operational ExecutionPlan in Postgres matches restored DecomposedTaskPlan.

    Compares immutable contract fields: node_id, sequence/order, goal/title, depends_on,
    task_spec, node_kind, aggregation_role, execution_mode, parallel_safe.
    Fails closed if the plan or node set is missing or mismatched.
    """
    if decomposed_plan.status != "decomposed":
        return

    if execution_plan is None or not getattr(execution_plan, "nodes", None):
        raise RuntimeError("ExecutionPlan missing or has no nodes for decomposed task.")

    plan_nodes = list(execution_plan.nodes)
    plan_nodes.sort(key=lambda n: getattr(n, "sequence_number", 0))

    if len(plan_nodes) != len(decomposed_plan.nodes):
        raise RuntimeError(
            f"ExecutionPlan node count ({len(plan_nodes)}) does not match "
            f"restored DecomposedTaskPlan node count ({len(decomposed_plan.nodes)})."
        )

    for seq_idx, (sql_node, model_node) in enumerate(
        zip(plan_nodes, decomposed_plan.nodes, strict=True)
    ):
        _validate_single_node_projection(sql_node, model_node, seq_idx)


def _reconstruct_single_node_outcome(
    node: Any,
    attempt: Any,
    marker: str,
) -> NodeOutcome:
    if attempt is not None and attempt.result_payload and isinstance(attempt.result_payload, dict):
        node_outcome_data = attempt.result_payload.get("node_outcome")
        if not isinstance(node_outcome_data, dict):
            raise RuntimeError(
                f"Malformed result_payload for node {node.node_id} attempt with key '{marker}'"
            )
        outcome = NodeOutcome.model_validate(node_outcome_data)
        return outcome.model_copy(
            update={
                "dependencies": list(node.depends_on or []),
                "logical_activity_key": marker,
                "result_digest": attempt.result_digest,
                "replayed": False,
            }
        )
    if (
        marker == getattr(node, "latest_logical_activity_key", None)
        and getattr(node, "terminal_result_payload", None)
        and isinstance(node.terminal_result_payload, dict)
    ):
        node_outcome_data = node.terminal_result_payload.get("node_outcome")
        if not isinstance(node_outcome_data, dict):
            raise RuntimeError(
                f"Malformed terminal_result_payload for node {node.node_id} with key '{marker}'"
            )
        outcome = NodeOutcome.model_validate(node_outcome_data)
        return outcome.model_copy(
            update={
                "dependencies": list(node.depends_on or []),
                "logical_activity_key": marker,
                "result_digest": getattr(node, "terminal_result_digest", None),
                "replayed": False,
            }
        )
    raise RuntimeError(
        f"Cannot reconstruct merged outcome for node {node.node_id}: marker '{marker}' "
        "has no durable attempt or terminal payload."
    )


def restore_merged_node_outcomes(
    execution_plan: Any,
    session: Any = None,
) -> list[NodeOutcome]:
    """Restore marker-confirmed merged node outcomes from durable relational state.

    Authority contract:
    - merged_logical_activity_key on ExecutionPlanNode identifies which attempt reached parent
      state.
    - ExecutionPlanNodeAttempt.result_payload is the immutable authority for worker attempts.
    - ExecutionPlanNode.terminal_result_payload is used only when
      marker == latest_logical_activity_key (supporting parent-generated terminal outcomes such
      as skips or synthetic missing evidence).
    - If a marker cannot be matched to a durable attempt or terminal payload, fails closed.
    """
    if execution_plan is None or not getattr(execution_plan, "nodes", None):
        return []

    merged_nodes = [
        node for node in execution_plan.nodes if getattr(node, "merged_logical_activity_key", None)
    ]
    if not merged_nodes:
        return []

    plan_node_ids = [node.id for node in merged_nodes if getattr(node, "id", None)]
    markers = [
        node.merged_logical_activity_key
        for node in merged_nodes
        if node.merged_logical_activity_key
    ]

    attempts_by_node_and_key: dict[tuple[str, str], Any] = {}
    if session is not None and plan_node_ids and markers:
        attempts = ExecutionPlanRepository(session).get_attempts_by_activity_keys(
            plan_node_ids=plan_node_ids,
            logical_activity_keys=markers,
        )
        for attempt in attempts:
            if attempt.logical_activity_key:
                attempts_by_node_and_key[(attempt.plan_node_id, attempt.logical_activity_key)] = (
                    attempt
                )
    else:
        for node in merged_nodes:
            node_id_val = getattr(node, "id", None)
            for attempt in getattr(node, "attempts", []) or []:
                key = getattr(attempt, "logical_activity_key", None)
                if node_id_val and key:
                    attempts_by_node_and_key[(node_id_val, key)] = attempt

    outcomes: list[NodeOutcome] = []
    for node in merged_nodes:
        marker = node.merged_logical_activity_key
        node_id_val = getattr(node, "id", None)
        attempt_match: Any = (
            attempts_by_node_and_key.get((node_id_val, marker))
            if (node_id_val and marker)
            else None
        )
        outcomes.append(_reconstruct_single_node_outcome(node, attempt_match, marker))

    return outcomes


def restore_decomposed_execution_state(
    execution_plan: Any,
    session: Any = None,
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

    merged_outcomes = restore_merged_node_outcomes(execution_plan, session=session)
    outcomes = [outcome.model_dump(mode="json") for outcome in merged_outcomes]
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
        "aggregation_role": node.aggregation_role or _aggregation_role(node.node_kind),
        "execution_mode": node.execution_mode or "mutable",
        "parallel_safe": bool(node.parallel_safe),
    }


def _aggregation_role(node_kind: str) -> str:
    if node_kind == "inspect":
        return "context"
    if node_kind == "verify":
        return "validation"
    return "mutation"
