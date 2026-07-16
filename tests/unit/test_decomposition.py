"""Tests for sequential M24 task decomposition."""

import pytest

from orchestrator.decomposition import decompose_task_plan, is_read_only_fanout_eligible
from orchestrator.state import DecomposedTaskNode, TaskPlan, TaskPlanStep, TaskSpec


def _plan(*steps: TaskPlanStep) -> TaskPlan:
    return TaskPlan(triggered=True, complexity_reason="multi_file_task", steps=list(steps))


def _step(step_id: str, outcome: str = "done", depends_on: list[str] | None = None) -> TaskPlanStep:
    return TaskPlanStep(
        step_id=step_id,
        title=f"Step {step_id}",
        expected_outcome=outcome,
        depends_on=depends_on,
    )


def _spec() -> TaskSpec:
    return TaskSpec(
        goal="Complete the change",
        acceptance_criteria=["The change is correct"],
        verification_commands=["pytest"],
    )


def test_decompose_task_plan_builds_sequential_dependencies() -> None:
    result = decompose_task_plan(
        _plan(_step("inspect"), _step("implement"), _step("verify")), _spec()
    )

    assert result.status == "decomposed"
    assert [node.node_id for node in result.nodes] == ["inspect", "implement", "verify"]
    assert [node.depends_on for node in result.nodes] == [[], ["inspect"], ["implement"]]
    assert result.nodes[0].task_spec.goal == "Step inspect"
    assert [node.node_kind for node in result.nodes] == ["inspect", "implement", "verify"]
    assert [node.aggregation_role for node in result.nodes] == [
        "context",
        "mutation",
        "validation",
    ]
    assert all(node.execution_mode == "mutable" for node in result.nodes)
    assert not any(node.parallel_safe for node in result.nodes)


def test_decompose_task_plan_preserves_explicit_branching() -> None:
    result = decompose_task_plan(
        _plan(
            _step("root"),
            _step("left", depends_on=["root"]),
            _step("right", depends_on=["root"]),
        ),
        _spec(),
    )

    assert result.status == "decomposed"
    assert result.nodes[1].depends_on == ["root"]
    assert result.nodes[2].depends_on == ["root"]


def test_decompose_task_plan_preserves_explicit_independent_root() -> None:
    result = decompose_task_plan(
        _plan(_step("root"), _step("independent", depends_on=[]), _step("joined", depends_on=[])),
        _spec(),
    )

    assert [node.depends_on for node in result.nodes] == [[], [], []]


def test_decompose_task_plan_preserves_explicit_node_classification() -> None:
    plan = _plan(
        TaskPlanStep(
            step_id="inspect",
            title="Inspect",
            expected_outcome="Find relevant code.",
            node_kind="inspect",
            aggregation_role="context",
            execution_mode="read_only",
            parallel_safe=True,
        )
    )

    result = decompose_task_plan(plan, _spec())

    assert result.nodes[0].node_kind == "inspect"
    assert result.nodes[0].aggregation_role == "context"
    assert result.nodes[0].execution_mode == "read_only"
    assert result.nodes[0].parallel_safe is True


@pytest.mark.parametrize(
    ("execution_mode", "aggregation_role"),
    [("mutable", "context"), ("read_only", "mutation"), ("read_only", None)],
)
def test_task_plan_step_rejects_unsafe_parallel_metadata(
    execution_mode: str, aggregation_role: str | None
) -> None:
    with pytest.raises(ValueError, match="parallel_safe steps"):
        TaskPlanStep(
            step_id="inspect",
            title="Inspect",
            expected_outcome="Find relevant code.",
            node_kind="inspect",
            execution_mode=execution_mode,
            aggregation_role=aggregation_role,
            parallel_safe=True,
        )


def test_read_only_fanout_eligibility_requires_every_safety_predicate() -> None:
    node = DecomposedTaskNode(
        node_id="inspect",
        title="Inspect",
        depends_on=["root"],
        task_spec=_spec(),
        node_kind="inspect",
        aggregation_role="context",
        execution_mode="read_only",
        parallel_safe=True,
    )
    eligible = {
        "parent_read_only": True,
        "selected_profile_mutation_policy": "read_only",
        "node": node,
        "completed_node_ids": {"root"},
        "has_unresolved_blocker": False,
        "fanout_disabled": False,
    }

    assert is_read_only_fanout_eligible(**eligible)
    assert not is_read_only_fanout_eligible(**(eligible | {"parent_read_only": False}))
    assert not is_read_only_fanout_eligible(
        **(eligible | {"selected_profile_mutation_policy": "patch_allowed"})
    )
    assert not is_read_only_fanout_eligible(**(eligible | {"completed_node_ids": set()}))
    assert not is_read_only_fanout_eligible(**(eligible | {"has_unresolved_blocker": True}))
    assert not is_read_only_fanout_eligible(**(eligible | {"fanout_disabled": True}))
    assert not is_read_only_fanout_eligible(
        **(eligible | {"node": node.model_copy(update={"parallel_safe": False})})
    )
    assert not is_read_only_fanout_eligible(
        **(eligible | {"node": node.model_copy(update={"execution_mode": "mutable"})})
    )
    assert not is_read_only_fanout_eligible(
        **(eligible | {"node": node.model_copy(update={"aggregation_role": "mutation"})})
    )


def test_decompose_task_plan_falls_back_for_invalid_graph() -> None:
    result = decompose_task_plan(
        _plan(_step("a", depends_on=["missing"]), _step("b", depends_on=["a"])),
        _spec(),
    )

    assert result.status == "fallback"
    assert result.nodes == []
    assert "missing_dependency:a:missing" in result.validation_errors


def test_decompose_task_plan_falls_back_for_cycle() -> None:
    result = decompose_task_plan(
        _plan(_step("a", depends_on=["b"]), _step("b", depends_on=["a"])),
        _spec(),
    )

    assert result.status == "fallback"
    assert result.validation_errors == ["dependency_cycle"]


def test_decompose_task_plan_falls_back_for_final_sequential_cycle() -> None:
    result = decompose_task_plan(
        _plan(_step("a", depends_on=["b"]), _step("b")),
        _spec(),
    )

    assert result.status == "fallback"
    assert result.validation_errors == ["dependency_cycle"]


def test_decompose_task_plan_falls_back_when_too_large() -> None:
    result = decompose_task_plan(
        _plan(*[_step(str(index)) for index in range(7)]),
        _spec(),
    )

    assert result.status == "fallback"
    assert result.reason == "node_count_exceeds_limit"


def test_decompose_task_plan_rejects_blank_verification_commands() -> None:
    spec = _spec().model_copy(update={"verification_commands": ["pytest", " "]})

    result = decompose_task_plan(_plan(_step("inspect")), spec)

    assert result.status == "fallback"
    assert result.validation_errors == ["invalid_verification_command"]


def test_decompose_task_plan_is_not_required_for_simple_tasks() -> None:
    result = decompose_task_plan(None, _spec())

    assert result.status == "not_required"
    assert result.reason == "task_plan_not_required"
