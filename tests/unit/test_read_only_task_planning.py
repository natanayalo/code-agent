"""Read-only task planning coverage for the bounded fan-out pilot."""

from orchestrator.nodes.ingestion import plan_task
from orchestrator.state import OrchestratorState


def test_qa_fanout_fixture_generates_parallel_safe_read_only_inspections() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Inspect documentation across files",
                "constraints": {"read_only": True},
            },
            "task_kind": "ambiguous",
        }
    )

    result = plan_task(state)

    steps = result["task_plan"]["steps"]
    assert [step["depends_on"] for step in steps] == [[], [], ["1", "2"]]
    assert [step["execution_mode"] for step in steps] == ["read_only"] * 3
    assert [step["parallel_safe"] for step in steps] == [True, True, False]
    assert [step["aggregation_role"] for step in steps] == ["context", "context", "validation"]


def test_read_only_request_retains_task_specific_production_plan() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Audit cancellation races in the webhook handler",
                "constraints": {"read_only": True},
            },
            "task_kind": "ambiguous",
        }
    )

    result = plan_task(state)

    assert result["task_plan"]["steps"][0]["title"] != "Inspect Repository Documentation"
    assert result["task_plan"]["steps"][1]["parallel_safe"] is True
