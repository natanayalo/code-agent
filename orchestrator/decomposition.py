"""Typed, deterministic decomposition of a task plan into executable nodes."""

from __future__ import annotations

from collections.abc import Mapping

from orchestrator.state import (
    AggregationRole,
    DecomposedTaskNode,
    DecomposedTaskPlan,
    NodeKind,
    TaskPlan,
    TaskSpec,
)

MAX_DECOMPOSED_NODES = 6


def decompose_task_plan(
    task_plan: TaskPlan | None,
    parent_spec: TaskSpec | None,
) -> DecomposedTaskPlan:
    """Convert the existing ordered plan into a validated sequential DAG."""
    if task_plan is None or not task_plan.triggered or parent_spec is None:
        return DecomposedTaskPlan(reason="task_plan_not_required")

    if len(task_plan.steps) > MAX_DECOMPOSED_NODES:
        return _fallback("node_count_exceeds_limit")
    if not task_plan.steps:
        return _fallback("task_plan_has_no_steps")

    step_ids = [step.step_id for step in task_plan.steps]
    errors: list[str] = []
    if len(step_ids) != len(set(step_ids)):
        errors.append("duplicate_node_ids")
    known_ids = set(step_ids)
    for step in task_plan.steps:
        if not step.expected_outcome.strip():
            errors.append(f"missing_acceptance_criteria:{step.step_id}")
        for dependency in step.depends_on or []:
            if dependency not in known_ids:
                errors.append(f"missing_dependency:{step.step_id}:{dependency}")
    if any(not command.strip() for command in parent_spec.verification_commands):
        errors.append("invalid_verification_command")

    if not errors:
        errors.extend(
            _cycle_errors({step.step_id: set(step.depends_on or []) for step in task_plan.steps})
        )
    if errors:
        return _fallback("invalid_task_plan", errors)

    nodes: list[DecomposedTaskNode] = []
    for index, step in enumerate(task_plan.steps):
        dependencies = list(step.depends_on or [])
        if step.depends_on is None and index > 0:
            dependencies = [task_plan.steps[index - 1].step_id]
        node_kind, aggregation_role = _node_metadata(
            step.node_kind, step.aggregation_role, index, len(task_plan.steps)
        )
        node_spec = parent_spec.model_copy(
            update={
                "goal": step.title,
                "acceptance_criteria": [step.expected_outcome],
            }
        )
        nodes.append(
            DecomposedTaskNode(
                node_id=step.step_id,
                title=step.title,
                depends_on=dependencies,
                task_spec=node_spec,
                node_kind=node_kind,
                expected_inputs=["parent_task_context", *dependencies],
                expected_outputs=["summary", "validation_evidence"],
                aggregation_role=aggregation_role,
                execution_mode=step.execution_mode,
                parallel_safe=step.parallel_safe,
            )
        )
    final_cycle_errors = _cycle_errors({node.node_id: set(node.depends_on) for node in nodes})
    if final_cycle_errors:
        return _fallback("invalid_task_plan", final_cycle_errors)
    return DecomposedTaskPlan(
        triggered=True,
        status="decomposed",
        reason=task_plan.complexity_reason or "complex_task",
        nodes=nodes,
    )


def _fallback(reason: str, errors: list[str] | None = None) -> DecomposedTaskPlan:
    return DecomposedTaskPlan(
        triggered=True,
        status="fallback",
        reason=reason,
        validation_errors=errors or [],
    )


def _node_metadata(
    node_kind: NodeKind | None,
    aggregation_role: AggregationRole | None,
    index: int,
    total: int,
) -> tuple[NodeKind, AggregationRole]:
    """Preserve legacy positional roles only for fields omitted by old plans."""
    legacy_kind, legacy_role = _legacy_node_metadata(index, total)
    return node_kind or legacy_kind, aggregation_role or legacy_role


def _legacy_node_metadata(index: int, total: int) -> tuple[NodeKind, AggregationRole]:
    if index == 0:
        return "inspect", "context"
    if index == total - 1:
        return "verify", "validation"
    return "implement", "mutation"


def _cycle_errors(dependencies: Mapping[str, set[str]]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        if any(visit(dependency) for dependency in dependencies[node_id]):
            return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return ["dependency_cycle"] if any(visit(node_id) for node_id in dependencies) else []


def is_read_only_fanout_eligible(
    *,
    parent_read_only: bool,
    selected_profile_mutation_policy: str | None,
    node: DecomposedTaskNode,
    completed_node_ids: set[str],
    has_unresolved_blocker: bool,
    fanout_disabled: bool,
) -> bool:
    """Return whether one ready node satisfies the future M25 fan-out contract."""
    return (
        parent_read_only
        and selected_profile_mutation_policy == "read_only"
        and node.execution_mode == "read_only"
        and node.parallel_safe
        and node.aggregation_role != "mutation"
        and all(dependency in completed_node_ids for dependency in node.depends_on)
        and not has_unresolved_blocker
        and not fanout_disabled
    )
