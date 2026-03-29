"""Integration tests for the LangGraph orchestrator skeleton."""

from __future__ import annotations

from orchestrator import OrchestratorState, WorkerResult, build_orchestrator_graph


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
