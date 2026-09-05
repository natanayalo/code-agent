"""Integration tests proving negated refactor instructions avoid architectural decomposition.

Covers the full path from ingestion normalization, classification, TaskSpec generation,
complexity reasoning, planning node execution, through downstream decomposition.
"""

from __future__ import annotations

import pytest

from orchestrator.decomposition import decompose_task_plan
from orchestrator.nodes.ingestion import classify_task, ingest_task, plan_task
from orchestrator.state import OrchestratorState, TaskPlan, TaskRequest
from orchestrator.task_spec import build_task_spec_for_request


def test_negated_refactor_bugfix_avoids_architectural_decomposition() -> None:
    """A bugfix containing 'Do not refactor adjacent logic' must avoid decomposition."""
    task_text = (
        "Fix zero banked free transfers handling in fpl_horizon/planning/optimizer.py, "
        "add deterministic regression coverage, and deliver a draft PR. "
        "Do NOT refactor adjacent logic."
    )
    initial_state = OrchestratorState.model_validate(
        {
            "task": TaskRequest(
                task_id="task-test-negated",
                task_text=task_text,
                repo_url="https://github.com/natanayalo/fpl-horizon",
                branch="master",
            ),
        }
    )

    # 1. Ingestion / normalization
    ingest_result = ingest_task(initial_state)
    state = initial_state.model_copy(
        update={"normalized_task_text": ingest_result["normalized_task_text"]}
    )

    # 2. Classification
    classify_result = classify_task(state)
    assert classify_result["task_kind"] == "implementation"
    assert classify_result["task_kind"] != "architecture"
    state = state.model_copy(update={"task_kind": classify_result["task_kind"]})

    # 3. Planning node (planning skipped because kind is implementation and no multi-file)
    plan_result = plan_task(state)
    assert plan_result["task_plan"] is None
    assert "planning skipped: task is straightforward" in plan_result["progress_updates"][-1]
    task_plan = (
        TaskPlan.model_validate(plan_result["task_plan"])
        if plan_result["task_plan"] is not None
        else None
    )
    state = state.model_copy(update={"task_plan": task_plan})

    # 4. TaskSpec generation
    task_spec = build_task_spec_for_request(
        state.task,
        task_kind=state.task_kind,
        task_plan=state.task_plan,
    )
    assert task_spec.task_type == "bugfix"
    assert task_spec.task_type != "refactor"
    state = state.model_copy(update={"task_spec": task_spec})

    # 5. Downstream decomposition: not required because task_plan is None
    decomp_result = decompose_task_plan(state.task_plan, task_spec)
    assert decomp_result.status == "not_required"
    assert decomp_result.reason == "task_plan_not_required"
    assert len(decomp_result.nodes) == 0


def test_affirmative_refactor_triggers_architectural_decomposition() -> None:
    """An affirmative refactoring request must produce a decomposed plan."""
    task_text = "Refactor architecture across files in orchestrator"
    initial_state = OrchestratorState.model_validate(
        {
            "task": TaskRequest(
                task_id="task-test-affirmative",
                task_text=task_text,
                repo_url="https://github.com/natanayalo/code-agent",
                branch="master",
            ),
        }
    )

    # 1. Ingestion / normalization
    ingest_result = ingest_task(initial_state)
    state = initial_state.model_copy(
        update={"normalized_task_text": ingest_result["normalized_task_text"]}
    )

    # 2. Classification
    classify_result = classify_task(state)
    assert classify_result["task_kind"] == "architecture"
    state = state.model_copy(update={"task_kind": classify_result["task_kind"]})

    # 3. Planning node
    plan_result = plan_task(state)
    assert plan_result["task_plan"] is not None
    assert plan_result["task_plan"]["triggered"] is True
    assert plan_result["task_plan"]["complexity_reason"] == "architectural_task"
    task_plan = TaskPlan.model_validate(plan_result["task_plan"])
    state = state.model_copy(update={"task_plan": task_plan})

    # 4. TaskSpec generation
    task_spec = build_task_spec_for_request(
        state.task,
        task_kind=state.task_kind,
        task_plan=state.task_plan,
    )
    assert task_spec.task_type == "refactor"
    state = state.model_copy(update={"task_spec": task_spec})

    # 5. Downstream decomposition
    decomp_result = decompose_task_plan(state.task_plan, task_spec)
    assert decomp_result.status == "decomposed"
    assert len(decomp_result.nodes) == 3


def test_mixed_refactor_and_negation_triggers_architectural_decomposition() -> None:
    """A task with both negative restrictions and affirmative refactoring requests decomposes."""
    task_text = (
        "Do not refactor the database layer. " "Refactor architecture across files in orchestrator."
    )
    initial_state = OrchestratorState.model_validate(
        {
            "task": TaskRequest(
                task_id="task-test-mixed",
                task_text=task_text,
                repo_url="https://github.com/natanayalo/code-agent",
                branch="master",
            ),
        }
    )

    # 1. Ingestion / normalization
    ingest_result = ingest_task(initial_state)
    state = initial_state.model_copy(
        update={"normalized_task_text": ingest_result["normalized_task_text"]}
    )

    # 2. Classification
    classify_result = classify_task(state)
    assert classify_result["task_kind"] == "architecture"
    state = state.model_copy(update={"task_kind": classify_result["task_kind"]})

    # 3. Planning node
    plan_result = plan_task(state)
    assert plan_result["task_plan"] is not None
    assert plan_result["task_plan"]["triggered"] is True
    assert plan_result["task_plan"]["complexity_reason"] == "architectural_task"
    task_plan = TaskPlan.model_validate(plan_result["task_plan"])
    state = state.model_copy(update={"task_plan": task_plan})

    # 4. TaskSpec generation
    task_spec = build_task_spec_for_request(
        state.task,
        task_kind=state.task_kind,
        task_plan=state.task_plan,
    )
    assert task_spec.task_type == "refactor"
    state = state.model_copy(update={"task_spec": task_spec})

    # 5. Downstream decomposition
    decomp_result = decompose_task_plan(state.task_plan, task_spec)
    assert decomp_result.status == "decomposed"
    assert len(decomp_result.nodes) == 3


@pytest.mark.parametrize(
    ("task_text", "expected_type"),
    [
        ("Fix memory leak in planning engine without refactoring.", "bugfix"),
        ("Fix solver constraint error. Avoid refactoring adjacent modules.", "bugfix"),
        ("Fix bug in optimizer. Never restructure existing models.", "bugfix"),
    ],
)
def test_negative_phrasing_variations_avoid_architectural_decomposition(
    task_text: str,
    expected_type: str,
) -> None:
    """Various phrasing styles for refactoring restrictions consistently avoid decomposition."""
    state = OrchestratorState.model_validate(
        {
            "task": TaskRequest(
                task_id="task-test-phrasing",
                task_text=task_text,
                repo_url="https://github.com/natanayalo/code-agent",
                branch="master",
            ),
        }
    )

    ingest_result = ingest_task(state)
    state = state.model_copy(update={"normalized_task_text": ingest_result["normalized_task_text"]})

    classify_result = classify_task(state)
    assert classify_result["task_kind"] == "implementation"
    state = state.model_copy(update={"task_kind": classify_result["task_kind"]})

    plan_result = plan_task(state)
    assert plan_result["task_plan"] is None
    task_plan = (
        TaskPlan.model_validate(plan_result["task_plan"])
        if plan_result["task_plan"] is not None
        else None
    )
    state = state.model_copy(update={"task_plan": task_plan})

    task_spec = build_task_spec_for_request(
        state.task,
        task_kind=state.task_kind,
        task_plan=state.task_plan,
    )
    assert task_spec.task_type == expected_type

    decomp_result = decompose_task_plan(state.task_plan, task_spec)
    assert decomp_result.status == "not_required"
