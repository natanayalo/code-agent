from types import SimpleNamespace

from orchestrator.execution_outcome_service import _apply_completion_control_constraints


def test_apply_completion_control_constraints_persists_only_repair_counters() -> None:
    task = SimpleNamespace(constraints={"operator_value": "preserved"})
    state = SimpleNamespace(
        task=SimpleNamespace(
            constraints={
                "independent_verifier_repair_passes_used": 1,
                "independent_review_repair_passes_used": 2,
                "independent_verifier_repair_request": "private repair prompt",
            }
        )
    )

    _apply_completion_control_constraints(task, state)

    assert task.constraints == {
        "operator_value": "preserved",
        "independent_verifier_repair_passes_used": 1,
        "independent_review_repair_passes_used": 2,
    }
