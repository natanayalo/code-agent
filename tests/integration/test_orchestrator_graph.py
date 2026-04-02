"""Integration tests for the LangGraph orchestrator skeleton."""

from __future__ import annotations

import asyncio
from pathlib import Path

from langgraph.types import Command

from orchestrator import OrchestratorState, WorkerResult, build_orchestrator_graph
from orchestrator.checkpoints import create_async_sqlite_checkpointer
from workers import Worker, WorkerRequest


class StaticWorker(Worker):
    """Test worker that returns a predefined result and records requests."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        return self.result


class UnexpectedWorker(Worker):
    """Test worker that should never be invoked."""

    def __init__(self, message: str) -> None:
        self.message = message

    async def run(self, request: WorkerRequest) -> WorkerResult:
        raise AssertionError(self.message)


def test_orchestrator_graph_runs_happy_path_with_fake_worker() -> None:
    """The compiled graph should complete the documented happy-path node sequence."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["orchestrator/graph.py"],
            test_results=[{"name": "fake-worker", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Add generic webhook endpoint",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert state.normalized_task_text == "Add generic webhook endpoint"
    assert state.task_kind == "implementation"
    assert state.route.chosen_worker == "codex"
    assert state.approval.required is False
    assert state.approval.status == "not_required"
    assert state.dispatch.worker_type == "codex"
    assert len(worker.requests) == 1
    assert worker.requests[0].task_text == "Add generic webhook endpoint"
    assert worker.requests[0].repo_url == "https://github.com/natanayalo/code-agent"
    assert worker.requests[0].branch == "master"
    assert state.result is not None
    assert state.result.status == "success"
    assert state.result.summary == "codex finished with status success"
    assert state.result.test_results[0].status == "passed"
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "memory context loaded",
        "worker selected: codex",
        "approval not required",
        "worker dispatched",
        "worker result received",
        "result summarized",
        "memory persistence queued",
    ]


def test_orchestrator_graph_resumes_from_persisted_sqlite_checkpoint(
    tmp_path: Path,
) -> None:
    """An interrupted graph can resume from a persisted SQLite checkpoint."""

    async def scenario() -> None:
        checkpoint_path = tmp_path / "orchestrator-checkpoints.sqlite"
        config = {"configurable": {"thread_id": "task-021"}}
        initial_input = {
            "task": {
                "task_text": "Add checkpoint persistence",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "master",
            }
        }

        unexpected_worker = UnexpectedWorker("await_result should not execute before resume.")

        async with create_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            interrupted_graph = build_orchestrator_graph(
                worker=unexpected_worker,
                checkpointer=checkpointer,
                interrupt_before=["await_result"],
            )

            await interrupted_graph.ainvoke(initial_input, config=config)
            snapshot = await interrupted_graph.aget_state(config)

        assert snapshot.next == ("await_result",)
        assert snapshot.values["current_step"] == "dispatch_job"
        assert snapshot.values["approval"]["status"] == "not_required"
        assert snapshot.values["dispatch"]["worker_type"] == "codex"
        assert snapshot.values.get("result") is None
        assert snapshot.values["progress_updates"] == [
            "task ingested",
            "task classified as implementation",
            "memory context loaded",
            "worker selected: codex",
            "approval not required",
            "worker dispatched",
        ]

        resumed_worker = StaticWorker(
            WorkerResult(
                status="success",
                commands_run=[],
                files_changed=["orchestrator/graph.py"],
                test_results=[{"name": "checkpoint-resume", "status": "passed"}],
                artifacts=[],
                next_action_hint="persist_memory",
                summary=None,
            )
        )
        async with create_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            resumed_graph = build_orchestrator_graph(
                worker=resumed_worker,
                checkpointer=checkpointer,
            )

            resumed_snapshot = await resumed_graph.aget_state(config)
            raw_output = await resumed_graph.ainvoke(None, config=config)

        assert resumed_snapshot.next == ("await_result",)

        state = OrchestratorState.model_validate(raw_output)

        assert state.current_step == "persist_memory"
        assert state.normalized_task_text == "Add checkpoint persistence"
        assert state.task_kind == "implementation"
        assert state.route.chosen_worker == "codex"
        assert state.approval.status == "not_required"
        assert state.dispatch.worker_type == "codex"
        assert len(resumed_worker.requests) == 1
        assert resumed_worker.requests[0].task_text == "Add checkpoint persistence"
        assert state.result is not None
        assert state.result.status == "success"
        assert state.result.summary == "codex finished with status success"
        assert state.result.test_results[0].status == "passed"
        assert state.progress_updates == [
            "task ingested",
            "task classified as implementation",
            "memory context loaded",
            "worker selected: codex",
            "approval not required",
            "worker dispatched",
            "worker result received",
            "result summarized",
            "memory persistence queued",
        ]

    asyncio.run(scenario())


def test_orchestrator_graph_interrupts_for_approval_and_resumes_cleanly(
    tmp_path: Path,
) -> None:
    """A destructive task should pause for approval and resume on confirmation."""

    async def scenario() -> None:
        checkpoint_path = tmp_path / "approval-checkpoints.sqlite"
        config = {"configurable": {"thread_id": "task-022-approved"}}
        initial_input = {
            "task": {
                "task_id": "task-022",
                "task_text": "Delete files from the repo workspace",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "master",
                "constraints": {
                    "requires_approval": True,
                    "approval_reason": "Task deletes files from the task workspace.",
                },
            }
        }

        worker = StaticWorker(
            WorkerResult(
                status="success",
                commands_run=[],
                files_changed=["sandbox/workspace.py"],
                test_results=[{"name": "approval-resume", "status": "passed"}],
                artifacts=[],
                next_action_hint="persist_memory",
                summary=None,
            )
        )
        async with create_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            graph = build_orchestrator_graph(
                worker=worker,
                checkpointer=checkpointer,
            )

            interrupted_output = await graph.ainvoke(initial_input, config=config)
            snapshot = await graph.aget_state(config)

            interrupts = interrupted_output["__interrupt__"]
            assert len(interrupts) == 1
            interrupt_payload = getattr(interrupts[0], "value")
            assert interrupt_payload["approval_type"] == "destructive_action"
            assert interrupt_payload["reason"] == "Task deletes files from the task workspace."
            assert interrupt_payload["resume_token"] == "approval-task-022"
            assert interrupt_payload["task_text"] == "Delete files from the repo workspace"
            assert snapshot.next == ("await_approval",)
            assert snapshot.values["current_step"] == "check_approval"
            assert snapshot.values["approval"]["status"] == "pending"
            assert snapshot.values["progress_updates"] == [
                "task ingested",
                "task classified as implementation",
                "memory context loaded",
                "worker selected: codex",
                "approval requested",
            ]

            raw_output = await graph.ainvoke(Command(resume=True), config=config)

        state = OrchestratorState.model_validate(raw_output)

        assert state.current_step == "persist_memory"
        assert state.approval.required is True
        assert state.approval.status == "approved"
        assert state.dispatch.worker_type == "codex"
        assert len(worker.requests) == 1
        assert worker.requests[0].task_text == "Delete files from the repo workspace"
        assert state.result is not None
        assert state.result.status == "success"
        assert state.result.test_results[0].name == "approval-resume"
        assert state.progress_updates == [
            "task ingested",
            "task classified as implementation",
            "memory context loaded",
            "worker selected: codex",
            "approval requested",
            "approval granted",
            "worker dispatched",
            "worker result received",
            "result summarized",
            "memory persistence queued",
        ]

    asyncio.run(scenario())


def test_orchestrator_graph_stops_when_approval_is_rejected(tmp_path: Path) -> None:
    """A rejected destructive task should not dispatch the worker."""

    async def scenario() -> None:
        checkpoint_path = tmp_path / "approval-rejected.sqlite"
        config = {"configurable": {"thread_id": "task-022-rejected"}}
        initial_input = {
            "task": {
                "task_id": "task-022-rejected",
                "task_text": "Delete files from the repo workspace",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "master",
                "constraints": {"requires_approval": True},
            }
        }

        unexpected_worker = UnexpectedWorker("dispatch should not run after approval is rejected.")

        async with create_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            graph = build_orchestrator_graph(
                worker=unexpected_worker,
                checkpointer=checkpointer,
            )

            await graph.ainvoke(initial_input, config=config)
            raw_output = await graph.ainvoke(Command(resume=False), config=config)

        state = OrchestratorState.model_validate(raw_output)

        assert state.current_step == "persist_memory"
        assert state.approval.required is True
        assert state.approval.status == "rejected"
        assert state.dispatch.run_id is None
        assert state.result is not None
        assert state.result.status == "failure"
        assert (
            state.result.summary
            == "Task halted because the requested destructive action was not approved."
        )
        assert state.progress_updates == [
            "task ingested",
            "task classified as implementation",
            "memory context loaded",
            "worker selected: codex",
            "approval requested",
            "approval rejected",
            "result summarized",
            "memory persistence queued",
        ]

    asyncio.run(scenario())
