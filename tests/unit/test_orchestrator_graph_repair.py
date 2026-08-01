# ruff: noqa: F403, F405
"""Focused graph coverage for completion-loop repair execution."""

from __future__ import annotations

from tests.unit.orchestrator_graph_unit_support import *  # noqa: F403


@pytest.mark.anyio
async def test_decomposed_repair_runs_one_monolithic_worker_and_preserves_node_evidence():
    class CapturingWorker(Worker):
        def __init__(self) -> None:
            self.requests: list[WorkerRequest] = []

        async def run(self, request: WorkerRequest, *, system_prompt=None) -> WorkerResult:
            self.requests.append(request)
            return WorkerResult(
                status="success",
                summary="repair completed",
                files_changed=["main.py"],
            )

    prior_outcome = NodeOutcome(
        node_id="implement",
        status="completed",
        result=WorkerResult(status="success", summary="initial DAG completed"),
    )
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Original decomposed task",
                "constraints": {"independent_verifier_repair_request": "Repair main.py"},
            },
            "route": {"chosen_worker": "codex", "route_reason": "default"},
            "dispatch": {"worker_type": "codex", "workspace_id": "retained-workspace"},
            "decomposed_plan": {"triggered": True, "status": "decomposed", "nodes": []},
            "node_outcomes": [prior_outcome.model_dump()],
            "current_node_id": "implement",
            "completion_loop": {
                "phase": "repair_requested",
                "repair_pass": 1,
                "repair_source": "verifier",
            },
        }
    )
    worker = CapturingWorker()

    response = await build_await_result_node(worker)(state)

    assert len(worker.requests) == 1
    assert worker.requests[0].task_text == "Repair main.py"
    assert worker.requests[0].workspace_id == "retained-workspace"
    assert response["node_outcomes"] == [prior_outcome.model_dump()]
    assert response["current_node_id"] == "implement"
