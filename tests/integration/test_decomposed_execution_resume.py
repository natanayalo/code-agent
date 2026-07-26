"""Integration coverage for DB-backed decomposed execution resume."""

from __future__ import annotations

import orchestrator.graph as graph_module
from orchestrator import OrchestratorState
from orchestrator.execution import TaskExecutionService
from orchestrator.execution_types import TaskSubmission
from orchestrator.graph import build_decompose_task_node
from repositories import ExecutionPlanRepository, session_scope
from workers import Worker, WorkerRequest, WorkerResult


class RecordingWorker(Worker):
    """Record resumed DAG node requests while returning successful node results."""

    def __init__(self) -> None:
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest, **kwargs) -> WorkerResult:
        del kwargs
        self.requests.append(request)
        return WorkerResult(
            status="success",
            summary=f"Completed {request.task_text}",
            files_changed=["qa-resume.txt"],
        )


def test_persisted_decomposition_skips_malformed_nodes(session_factory, monkeypatch) -> None:
    service = TaskExecutionService(session_factory=session_factory, worker=RecordingWorker())
    snapshot, _ = service.create_task(TaskSubmission(task_text="Persist a safe DAG"))
    node = build_decompose_task_node(session_factory)
    response = {
        "decomposed_plan": {
            "status": "decomposed",
            "nodes": [
                None,
                {"title": "Missing ID"},
                {
                    "node_id": "valid",
                    "title": "Valid",
                    "task_spec": {"goal": "Valid"},
                    "node_kind": "inspect",
                    "aggregation_role": "context",
                    "execution_mode": "read_only",
                    "parallel_safe": True,
                },
                {"node_id": "blank-title", "title": "", "task_spec": {"goal": "Blank"}},
            ],
        }
    }
    monkeypatch.setattr(graph_module, "decompose_task", lambda state: response)

    node(
        OrchestratorState.model_validate(
            {"task": {"task_id": snapshot.task_id, "task_text": "Persist a safe DAG"}}
        )
    )

    with session_scope(session_factory) as session:
        plan = ExecutionPlanRepository(session).get_by_task_id(snapshot.task_id)
        assert plan is not None
        assert [plan_node.node_id for plan_node in plan.nodes] == ["valid", "blank-title"]
        node_goals = {plan_node.node_id: plan_node.goal for plan_node in plan.nodes}
        assert node_goals["blank-title"] == "blank-title"
        valid_node = next(plan_node for plan_node in plan.nodes if plan_node.node_id == "valid")
        assert valid_node.node_kind == "inspect"
        assert valid_node.aggregation_role == "context"
        assert valid_node.execution_mode == "read_only"
        assert valid_node.parallel_safe is True
