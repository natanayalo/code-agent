"""Tests for sequential M24 task decomposition."""

from orchestrator.decomposition import decompose_task_plan
from orchestrator.state import TaskPlan, TaskPlanStep, TaskSpec


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
    assert result.nodes[-1].node_kind == "verify"


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
