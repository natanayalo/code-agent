"""Integration tests for orchestrator graph failure and cancellation paths."""

from __future__ import annotations

import asyncio

from orchestrator import OrchestratorState, build_orchestrator_graph
from tests.integration.orchestrator_graph_support import (
    CleanupCrashingWorker,
    CrashingWorker,
    SlowWorker,
)


def test_orchestrator_graph_returns_a_structured_timeout_result() -> None:
    """The outer orchestrator timeout should fail safely instead of hanging forever."""
    worker = SlowWorker(delay_seconds=5)
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run the slow worker path",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "budget": {"orchestrator_timeout_seconds": 1},
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert len(worker.requests) == 1
    assert worker.cancelled is True
    assert state.result is not None
    assert state.result.status == "failure"
    assert state.result.summary == (
        "Worker execution exceeded the orchestrator timeout envelope (1s) and was cancelled."
    )
    assert state.result.next_action_hint == "inspect_workspace_artifacts"
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "planning skipped: task is straightforward",
        "task spec and route generated",
        "memory context loaded",
        "approval not required",
        "worker dispatched",
        "worker timed out after 1s",
        "verification failed",
        "independent code-change review skipped (no files changed)",
        "result summarized and session state updated",
        "memory persistence queued",
    ]


def test_orchestrator_graph_surfaces_worker_cancellation_as_a_result() -> None:
    """Cancelling the graph during worker execution should still produce a typed failure."""

    async def scenario() -> None:
        worker = SlowWorker(delay_seconds=5)
        graph = build_orchestrator_graph(worker=worker)

        graph_task = asyncio.create_task(
            graph.ainvoke(
                {
                    "task": {
                        "task_text": "Cancel the worker path",
                        "repo_url": "https://github.com/natanayalo/code-agent",
                        "branch": "master",
                    }
                }
            )
        )

        for _ in range(100):
            if worker.requests:
                break
            await asyncio.sleep(0.01)
        assert worker.requests, "Worker never started before cancellation."

        graph_task.cancel()
        raw_output = await graph_task
        state = OrchestratorState.model_validate(raw_output)

        assert state.current_step == "persist_memory"
        assert worker.cancelled is True
        assert state.result is not None
        assert state.result.status == "failure"
        assert state.result.summary == (
            "Worker execution was cancelled before it returned a result."
        )
        assert state.result.next_action_hint == "await_manual_follow_up"
        assert state.progress_updates == [
            "task ingested",
            "task classified as implementation",
            "planning skipped: task is straightforward",
            "task spec and route generated",
            "memory context loaded",
            "approval not required",
            "worker dispatched",
            "worker execution cancelled",
            "verification failed",
            "independent code-change review skipped (no files changed)",
            "result summarized and session state updated",
            "memory persistence queued",
        ]

    asyncio.run(scenario())


def test_orchestrator_graph_returns_a_structured_error_for_worker_crashes() -> None:
    """Unexpected worker exceptions should not crash the orchestrator graph."""
    worker = CrashingWorker("adapter exploded")
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run the crashing worker path",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert len(worker.requests) == 1
    assert state.result is not None
    assert state.result.status == "error"
    assert state.result.summary == (
        "Worker execution crashed unexpectedly: RuntimeError: adapter exploded"
    )
    assert state.result.next_action_hint == "inspect_worker_configuration"
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "planning skipped: task is straightforward",
        "task spec and route generated",
        "memory context loaded",
        "approval not required",
        "worker dispatched",
        "worker crashed unexpectedly",
        "verification failed",
        "independent code-change review skipped (no files changed)",
        "result summarized and session state updated",
        "memory persistence queued",
    ]


def test_orchestrator_graph_timeout_path_tolerates_cleanup_exceptions() -> None:
    """A worker that fails while processing cancellation should not crash cleanup."""
    worker = CleanupCrashingWorker(delay_seconds=5)
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run the cleanup-crashing worker path",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "budget": {"orchestrator_timeout_seconds": 1},
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert len(worker.requests) == 1
    assert worker.cleanup_failed is True
    assert state.result is not None
    assert state.result.status == "failure"
    assert state.result.summary == (
        "Worker execution exceeded the orchestrator timeout envelope (1s) and was cancelled."
    )
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "planning skipped: task is straightforward",
        "task spec and route generated",
        "memory context loaded",
        "approval not required",
        "worker dispatched",
        "worker timed out after 1s",
        "verification failed",
        "independent code-change review skipped (no files changed)",
        "result summarized and session state updated",
        "memory persistence queued",
    ]
