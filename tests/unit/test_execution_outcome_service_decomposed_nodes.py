"""Regression tests for decomposed node outcome persistence."""

from __future__ import annotations

from datetime import UTC, datetime

from db.enums import ExecutionPlanNodeStatus
from orchestrator.execution_outcome_service import _persist_decomposed_node_outcomes
from orchestrator.state import NodeOutcome, OrchestratorState
from workers import WorkerResult


def test_persist_decomposed_node_outcomes_handles_nullable_result_lists() -> None:
    class RecordingPlanRepo:
        def __init__(self) -> None:
            self.updated: dict[str, object] | None = None

        def update_node(self, **kwargs):
            self.updated = kwargs

    repo = RecordingPlanRepo()
    result = WorkerResult.model_construct(
        status="success",
        summary="Completed",
        failure_kind=None,
        test_results=None,
        artifacts=None,
        files_changed=[],
    )
    state = OrchestratorState.model_construct(
        node_outcomes=[
            NodeOutcome.model_construct(
                node_id="inspect",
                status="completed",
                result=result,
                dependencies=[],
                attempts=1,
            )
        ]
    )

    _persist_decomposed_node_outcomes(
        plan_repo=repo,
        plan_id="plan-1",
        state=state,
        worker_run_id="run-1",
        finished_at=datetime(2026, 7, 10, tzinfo=UTC),
    )

    assert repo.updated is not None
    assert repo.updated["status"] == ExecutionPlanNodeStatus.COMPLETED
    assert repo.updated["verification_outcome"] == {
        "status": "passed",
        "test_results": [],
    }
    assert repo.updated["output_artifacts"] == []
