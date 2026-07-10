# ruff: noqa: F403, F405
"""Tests for M24 sequential node execution and aggregation."""

from __future__ import annotations

import asyncio

from tests.unit.orchestrator_graph_unit_support import *  # noqa: F403, F405


def test_aggregate_decomposed_results_deduplicates_changed_files():
    outcomes = [
        NodeOutcome(
            node_id="inspect",
            status="completed",
            result=WorkerResult(
                status="success",
                summary="Inspected",
                files_changed=["src/example.py"],
                memory_to_persist=[
                    {
                        "category": "project",
                        "memory_key": "test_command",
                        "value": {"command": "pytest"},
                    }
                ],
                delivery_metadata={"branch_name": "feature/example"},
            ),
        ),
        NodeOutcome(
            node_id="verify",
            status="completed",
            result=WorkerResult(
                status="success",
                summary="Verified",
                files_changed=["src/example.py", "tests/test_example.py"],
            ),
        ),
    ]

    result = _aggregate_decomposed_results(outcomes)

    assert result.status == "success"
    assert result.files_changed == ["src/example.py", "tests/test_example.py"]
    assert "inspect: Inspected" in (result.summary or "")
    assert result.memory_to_persist[0].memory_key == "test_command"
    assert result.delivery_metadata == {"branch_name": "feature/example"}


def test_aggregate_decomposed_results_blocks_success_when_node_is_skipped():
    outcomes = [
        NodeOutcome(
            node_id="implement",
            status="skipped",
            result=WorkerResult(
                status="failure",
                summary="Skipped",
                failure_kind="incomplete_delivery",
            ),
        )
    ]

    result = _aggregate_decomposed_results(outcomes)

    assert result.status == "failure"
    assert result.next_action_hint == "inspect_failed_node"


def test_decomposed_permission_block_resumes_from_blocked_node():
    class PermissionThenSuccessWorker(Worker):
        def __init__(self):
            self.calls = 0

        async def run(self, request: WorkerRequest, **kwargs) -> WorkerResult:
            del request, kwargs
            self.calls += 1
            if self.calls == 1:
                return WorkerResult(
                    status="failure",
                    failure_kind="permission_denied",
                    requested_permission="dangerous_shell",
                    next_action_hint="request_higher_permission",
                )
            return WorkerResult(status="success", summary="Node complete")

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Implement and verify"},
            "task_spec": {"goal": "Implement and verify"},
            "route": {"chosen_worker": "codex"},
            "dispatch": {"worker_type": "codex"},
            "decomposed_plan": {
                "triggered": True,
                "status": "decomposed",
                "nodes": [
                    {
                        "node_id": "implement",
                        "title": "Implement",
                        "task_spec": {"goal": "Implement"},
                        "node_kind": "implement",
                    },
                    {
                        "node_id": "verify",
                        "title": "Verify",
                        "depends_on": ["implement"],
                        "task_spec": {"goal": "Verify"},
                        "node_kind": "verify",
                    },
                ],
            },
        }
    )
    worker = PermissionThenSuccessWorker()

    first_result, first_outcomes, _ = asyncio.run(_await_decomposed_nodes(state, worker))

    assert first_result.next_action_hint == "request_higher_permission"
    assert first_outcomes[0].status == "blocked"
    assert len(first_outcomes) == 1

    resumed_state = state.model_copy(update={"node_outcomes": first_outcomes})
    resumed_result, resumed_outcomes, _ = asyncio.run(
        _await_decomposed_nodes(resumed_state, worker)
    )

    assert resumed_result.status == "success"
    assert [outcome.node_id for outcome in resumed_outcomes] == ["implement", "verify"]
    assert all(outcome.status == "completed" for outcome in resumed_outcomes)
    assert worker.calls == 3


def test_decomposed_resume_retries_skipped_downstream_nodes():
    class SuccessWorker(Worker):
        def __init__(self):
            self.task_texts: list[str] = []

        async def run(self, request: WorkerRequest, **kwargs) -> WorkerResult:
            del kwargs
            self.task_texts.append(request.task_text)
            return WorkerResult(status="success", summary=f"Completed {request.task_text}")

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Implement and verify"},
            "task_spec": {"goal": "Implement and verify"},
            "route": {"chosen_worker": "codex"},
            "dispatch": {"worker_type": "codex"},
            "decomposed_plan": {
                "triggered": True,
                "status": "decomposed",
                "nodes": [
                    {
                        "node_id": "implement",
                        "title": "Implement",
                        "task_spec": {"goal": "Implement"},
                        "node_kind": "implement",
                    },
                    {
                        "node_id": "verify",
                        "title": "Verify",
                        "depends_on": ["implement"],
                        "task_spec": {"goal": "Verify"},
                        "node_kind": "verify",
                    },
                ],
            },
            "node_outcomes": [
                {
                    "node_id": "implement",
                    "status": "failed",
                    "result": {
                        "status": "failure",
                        "summary": "Previous failure",
                        "failure_kind": "worker_failure",
                    },
                },
                {
                    "node_id": "verify",
                    "status": "skipped",
                    "result": {
                        "status": "failure",
                        "summary": "Previous skip",
                        "failure_kind": "incomplete_delivery",
                    },
                    "dependencies": ["implement"],
                },
            ],
        }
    )
    worker = SuccessWorker()

    result, outcomes, _ = asyncio.run(_await_decomposed_nodes(state, worker))

    assert result.status == "success"
    assert worker.task_texts == ["Implement", "Verify"]
    assert [(outcome.node_id, outcome.status) for outcome in outcomes] == [
        ("implement", "completed"),
        ("verify", "completed"),
    ]
