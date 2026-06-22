"""Integration tests for normal orchestrator graph execution flows."""

from __future__ import annotations

import asyncio

from orchestrator import OrchestratorState, WorkerResult, build_orchestrator_graph
from tests.integration.orchestrator_graph_support import SequencedWorker, StaticWorker


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
    assert state.task_spec is not None
    assert state.task_spec.goal == "Add generic webhook endpoint"
    assert state.route.chosen_worker == "codex"
    assert state.approval.required is False
    assert state.approval.status == "not_required"
    assert state.dispatch.worker_type == "codex"
    assert state.dispatch.run_id is None
    assert state.dispatch.workspace_id is None
    assert len(worker.requests) == 1
    assert worker.requests[0].task_text == "Add generic webhook endpoint"
    assert worker.requests[0].repo_url == "https://github.com/natanayalo/code-agent"
    assert worker.requests[0].branch == "master"
    assert worker.requests[0].task_spec is not None
    assert worker.requests[0].task_spec["goal"] == "Add generic webhook endpoint"
    assert state.result is not None
    assert state.result.status == "success"
    assert state.result.summary == "codex finished with status success"
    assert state.result.test_results[0].status == "passed"
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "planning skipped: task is straightforward",
        "task spec and route generated",
        "memory context loaded",
        "approval not required",
        "worker dispatched",
        "worker result received",
        "verification passed",
        "result summarized and session state updated",
        "memory persistence queued",
    ]


def test_orchestrator_graph_runs_one_verifier_repair_handoff_then_stops() -> None:
    worker = SequencedWorker(
        [
            WorkerResult(
                status="success",
                summary="Initial implementation finished.",
                commands_run=[],
                files_changed=["orchestrator/graph.py"],
                test_results=[{"name": "unit", "status": "failed"}],
                artifacts=[],
                next_action_hint="persist_memory",
            ),
            WorkerResult(
                status="success",
                summary="Applied verifier repair follow-up.",
                commands_run=[],
                files_changed=["orchestrator/graph.py"],
                test_results=[{"name": "unit", "status": "failed"}],
                artifacts=[],
                next_action_hint="persist_memory",
            ),
        ]
    )
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Fix verifier repair behavior",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert len(worker.requests) == 2
    assert worker.requests[1].task_text.startswith(
        "Apply targeted code fixes for failed verification checks."
    )
    assert state.verification is not None
    assert state.verification.status == "failed"
    assert state.result is not None
    assert state.result.next_action_hint == "await_manual_follow_up"
    assert "Verification is still failing after 1 bounded repair attempt" in (
        state.result.summary or ""
    )
    assert any(
        "verification failed; queued bounded repair handoff (1/1)" in update
        for update in state.progress_updates
    )
    assert any(
        "verification failed after bounded repair attempts" in update
        for update in state.progress_updates
    )


def test_orchestrator_graph_clarification_resume_token_resolution_allows_progress() -> None:
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["artifacts/review.md"],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="Done",
        )
    )
    graph = build_orchestrator_graph(worker=worker)
    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_id": "task-clar-1",
                    "task_text": "Review orchestrator and summarize findings",
                    "constraints": {
                        "interactions": {
                            "old-hash": {
                                "status": "resolved",
                                "interaction_type": "clarification",
                                "data": {
                                    "source": "task_spec",
                                    "resume_token": "clarification-task-clar-1",
                                    "questions": ["old question"],
                                },
                            }
                        }
                    },
                },
                "task_spec": {
                    "goal": "Review orchestrator and summarize findings",
                    "task_type": "investigation",
                    "risk_level": "low",
                    "delivery_mode": "workspace",
                    "allowed_actions": ["modify_workspace_files"],
                    "forbidden_actions": ["hardcode_secrets"],
                    "requires_clarification": True,
                    "clarification_questions": ["new question wording"],
                    "requires_permission": False,
                },
                "task_kind": "implementation",
                "session": {
                    "session_id": "s1",
                    "user_id": "u1",
                    "channel": "http",
                    "external_thread_id": "t1",
                },
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)
    assert state.result is not None
    assert state.result.status == "success"
    assert len(worker.requests) == 1


def test_orchestrator_graph_review_task_without_deliverable_fails() -> None:
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="Completed.",
        )
    )
    graph = build_orchestrator_graph(worker=worker)
    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": (
                        "Review the orchestrator implementation and compare to best practices"
                    ),
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)
    assert state.verification is not None
    assert state.verification.status == "failed"
    assert state.verification.failure_kind == "incomplete_delivery"
    assert state.result is not None
    assert state.result.status == "failure"


def test_orchestrator_graph_scout_task_skips_delivery() -> None:
    """Scout tasks should process normally but skip branch delivery entirely."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="Scout complete.",
        )
    )
    graph = build_orchestrator_graph(worker=worker)
    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Scout the orchestrator module",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "constraints": {"task_type": "scout", "delivery_mode": "branch"},
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)
    assert state.task_spec is not None
    assert state.task_spec.task_type == "scout"
    assert state.task_spec.delivery_mode == "summary"
    assert state.result is not None
    assert state.result.status == "success"
    assert len(worker.requests) == 1
    assert "delivery completed" not in state.progress_updates
