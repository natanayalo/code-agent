"""Integration tests for the LangGraph orchestrator skeleton."""

from __future__ import annotations

from pathlib import Path

from orchestrator import OrchestratorState, WorkerResult, build_orchestrator_graph
from orchestrator.checkpoints import create_sqlite_checkpointer


def test_orchestrator_graph_runs_happy_path_with_fake_worker() -> None:
    """The compiled graph should complete the documented happy-path node sequence."""
    graph = build_orchestrator_graph(
        worker_result_provider=lambda state: WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["orchestrator/graph.py"],
            test_results=[{"name": "fake-worker", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )

    raw_output = graph.invoke(
        {
            "task": {
                "task_text": "Add generic webhook endpoint",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "master",
            }
        }
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert state.normalized_task_text == "Add generic webhook endpoint"
    assert state.task_kind == "implementation"
    assert state.route.chosen_worker == "codex"
    assert state.dispatch.worker_type == "codex"
    assert state.result is not None
    assert state.result.status == "success"
    assert state.result.summary == "codex finished with status success"
    assert state.result.test_results[0].status == "passed"
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "memory context loaded",
        "worker selected: codex",
        "worker dispatched",
        "worker result received",
        "result summarized",
        "memory persistence queued",
    ]


def test_orchestrator_graph_resumes_from_persisted_sqlite_checkpoint(
    tmp_path: Path,
) -> None:
    """An interrupted graph can resume from a persisted SQLite checkpoint."""

    checkpoint_path = tmp_path / "orchestrator-checkpoints.sqlite"
    config = {"configurable": {"thread_id": "task-021"}}
    initial_input = {
        "task": {
            "task_text": "Add checkpoint persistence",
            "repo_url": "https://github.com/natanayalo/code-agent",
            "branch": "master",
        }
    }

    def unexpected_worker_result(_state: OrchestratorState) -> WorkerResult:
        raise AssertionError("await_result should not execute before resume.")

    with create_sqlite_checkpointer(checkpoint_path) as checkpointer:
        interrupted_graph = build_orchestrator_graph(
            worker_result_provider=unexpected_worker_result,
            checkpointer=checkpointer,
            interrupt_before=["await_result"],
        )

        interrupted_graph.invoke(initial_input, config=config)
        snapshot = interrupted_graph.get_state(config)

    assert snapshot.next == ("await_result",)
    assert snapshot.values["current_step"] == "dispatch_job"
    assert snapshot.values["dispatch"]["worker_type"] == "codex"
    assert snapshot.values.get("result") is None
    assert snapshot.values["progress_updates"] == [
        "task ingested",
        "task classified as implementation",
        "memory context loaded",
        "worker selected: codex",
        "worker dispatched",
    ]

    with create_sqlite_checkpointer(checkpoint_path) as checkpointer:
        resumed_graph = build_orchestrator_graph(
            worker_result_provider=lambda _state: WorkerResult(
                status="success",
                commands_run=[],
                files_changed=["orchestrator/graph.py"],
                test_results=[{"name": "checkpoint-resume", "status": "passed"}],
                artifacts=[],
                next_action_hint="persist_memory",
                summary=None,
            ),
            checkpointer=checkpointer,
        )

        resumed_snapshot = resumed_graph.get_state(config)
        raw_output = resumed_graph.invoke(None, config=config)

    assert resumed_snapshot.next == ("await_result",)

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert state.normalized_task_text == "Add checkpoint persistence"
    assert state.task_kind == "implementation"
    assert state.route.chosen_worker == "codex"
    assert state.dispatch.worker_type == "codex"
    assert state.result is not None
    assert state.result.status == "success"
    assert state.result.summary == "codex finished with status success"
    assert state.result.test_results[0].status == "passed"
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "memory context loaded",
        "worker selected: codex",
        "worker dispatched",
        "worker result received",
        "result summarized",
        "memory persistence queued",
    ]
