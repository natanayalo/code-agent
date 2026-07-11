"""Integration coverage for DB-backed decomposed execution resume."""

from __future__ import annotations

import asyncio

import orchestrator.graph as graph_module
from db.enums import ExecutionPlanNodeStatus
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
                {"node_id": "valid", "title": "Valid", "task_spec": {"goal": "Valid"}},
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
        assert [plan_node.node_id for plan_node in plan.nodes] == ["valid"]


def _assert_persisted_attempts(service: TaskExecutionService, task_id: str) -> None:
    """Assert resumed nodes retain one durable attempt record each."""
    persisted_snapshot = service.get_task(task_id)
    assert persisted_snapshot is not None
    nodes = {node.node_id: node for node in persisted_snapshot.execution_plan.nodes}
    assert [attempt.attempt_number for attempt in nodes["2"].attempts] == [1]
    assert nodes["2"].attempts[0].status == "completed"
    assert nodes["2"].attempts[0].effective_input_digest
    assert [attempt.attempt_number for attempt in nodes["3"].attempts] == [1]


def test_queue_reload_resumes_only_non_completed_decomposed_nodes(session_factory) -> None:
    worker = RecordingWorker()
    service = TaskExecutionService(session_factory=session_factory, worker=worker)
    snapshot, _persisted = service.create_task(
        TaskSubmission(task_text="Implement a multi-file change and verify it")
    )

    with session_scope(session_factory) as session:
        plan_repo = ExecutionPlanRepository(session)
        plan = plan_repo.create(task_id=snapshot.task_id)
        plan_repo.add_node(
            plan_id=plan.id,
            node_id="1",
            goal="Inspect",
            task_spec={"goal": "Inspect"},
            node_kind="inspect",
            status=ExecutionPlanNodeStatus.COMPLETED,
        )
        plan_repo.add_node(
            plan_id=plan.id,
            node_id="2",
            goal="Implement",
            depends_on=["1"],
            task_spec={"goal": "Implement"},
            node_kind="implement",
            status=ExecutionPlanNodeStatus.FAILED,
        )
        plan_repo.add_node(
            plan_id=plan.id,
            node_id="3",
            goal="Verify",
            depends_on=["2"],
            task_spec={"goal": "Verify"},
            node_kind="verify",
            status=ExecutionPlanNodeStatus.SKIPPED,
        )
        plan_repo.update_node(
            plan_id=plan.id,
            node_id="1",
            result_summary="Inspection completed.",
            changed_files=["README.md"],
        )
        plan_repo.update_node(
            plan_id=plan.id,
            node_id="2",
            result_summary="Implementation failed previously.",
            failure_kind="worker_failure",
        )
        plan_repo.update_node(
            plan_id=plan.id,
            node_id="3",
            result_summary="Verification was skipped.",
            failure_kind="incomplete_delivery",
        )

    loaded = service._load_submission_for_task(task_id=snapshot.task_id)

    assert loaded is not None
    submission, persisted = loaded
    assert persisted.decomposed_plan is not None
    assert [node["node_id"] for node in persisted.decomposed_plan["nodes"]] == ["1", "2", "3"]
    assert [outcome["node_id"] for outcome in persisted.node_outcomes] == ["1", "2", "3"]

    state = asyncio.run(service._run_orchestrator(submission, persisted))

    assert state.result is not None
    assert state.result.status == "success"
    dag_requests = [
        request for request in worker.requests if "Current DAG node" in request.task_text
    ]
    assert len(dag_requests) == 2
    assert "Current DAG node (implement)" in dag_requests[0].task_text
    assert "Current DAG node (verify)" in dag_requests[1].task_text
    assert all("Current DAG node (inspect)" not in request.task_text for request in dag_requests)

    _assert_persisted_attempts(service, snapshot.task_id)
