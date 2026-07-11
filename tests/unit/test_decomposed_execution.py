# ruff: noqa: F403, F405
"""Tests for M24 sequential node execution and aggregation."""

from __future__ import annotations

import asyncio

from tests.unit.orchestrator_graph_unit_support import *  # noqa: F403, F405


def test_redact_effective_input_redacts_sensitive_key_substrings() -> None:
    result = _redact_effective_input(
        {"github_token": "secret", "db_password": "secret", "safe_value": "visible"}, set()
    )

    assert result == {
        "github_token": "[REDACTED]",
        "db_password": "[REDACTED]",
        "safe_value": "visible",
    }


def test_effective_input_evidence_tolerates_missing_node_task_spec() -> None:
    state = OrchestratorState.model_validate({"task": {"task_text": "Run a task"}})
    node = DecomposedTaskNode.model_construct(
        node_id="inspect", title="Inspect", node_kind="inspect", task_spec=None
    )

    summary, _ = _effective_input_evidence(state, node, {})

    assert summary["goal"] == ""
    assert summary["acceptance_criteria"] == []


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


def test_aggregate_decomposed_results_handles_nullable_result_lists():
    outcome = NodeOutcome.model_construct(
        node_id="inspect",
        status="completed",
        result=WorkerResult.model_construct(
            status="success",
            summary="Inspected",
            commands_run=None,
            files_changed=None,
            test_results=None,
            artifacts=None,
            friction_reports=None,
            maintenance_requests=None,
            memory_to_persist=None,
        ),
        dependencies=[],
        attempts=1,
    )

    result = _aggregate_decomposed_results([outcome])

    assert result.status == "success"
    assert result.commands_run == []
    assert result.files_changed == []
    assert result.test_results == []
    assert result.artifacts == []
    assert result.friction_reports == []
    assert result.maintenance_requests == []
    assert result.memory_to_persist == []


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
                        "max_attempts": 3,
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
    assert worker.calls == 1

    resumed_state = state.model_copy(update={"node_outcomes": first_outcomes})
    resumed_result, resumed_outcomes, _ = asyncio.run(
        _await_decomposed_nodes(resumed_state, worker)
    )

    assert resumed_result.status == "success"
    assert [outcome.node_id for outcome in resumed_outcomes] == ["implement", "verify"]
    assert all(outcome.status == "completed" for outcome in resumed_outcomes)
    assert worker.calls == 3


def test_decomposed_node_none_result_falls_back_to_failure():
    class NoneWorker(Worker):
        async def run(self, request: WorkerRequest, **kwargs) -> WorkerResult:
            del request, kwargs
            return None  # type: ignore[return-value]

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Implement"},
            "task_spec": {"goal": "Implement"},
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
                ],
            },
        }
    )

    result, outcomes, _ = asyncio.run(_await_decomposed_nodes(state, NoneWorker()))

    assert result.status == "failure"
    assert outcomes[0].status == "failed"
    assert outcomes[0].result.failure_kind == "worker_failure"


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
    assert all("Parent task:\nImplement and verify" in task_text for task_text in worker.task_texts)
    assert "Current DAG node (implement): Implement" in worker.task_texts[0]
    assert "Current DAG node (verify): Verify" in worker.task_texts[1]
    assert [(outcome.node_id, outcome.status) for outcome in outcomes] == [
        ("implement", "completed"),
        ("verify", "completed"),
    ]


def test_decomposed_prior_context_handles_nullable_dependency_artifacts():
    class ContextAssertingWorker(Worker):
        async def run(self, request: WorkerRequest, **kwargs) -> WorkerResult:
            del kwargs
            prior_context = request.memory_context["decomposed_task"]
            assert prior_context["inspect"]["files_changed"] == []
            assert prior_context["inspect"]["artifacts"] == []
            return WorkerResult(status="success", summary="Verified")

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Verify after inspection"},
            "task_spec": {"goal": "Verify after inspection"},
            "route": {"chosen_worker": "codex"},
            "dispatch": {"worker_type": "codex"},
            "decomposed_plan": {
                "triggered": True,
                "status": "decomposed",
                "nodes": [
                    {
                        "node_id": "inspect",
                        "title": "Inspect",
                        "task_spec": {"goal": "Inspect"},
                        "node_kind": "inspect",
                    },
                    {
                        "node_id": "verify",
                        "title": "Verify",
                        "depends_on": ["inspect"],
                        "task_spec": {"goal": "Verify"},
                        "node_kind": "verify",
                    },
                ],
            },
        }
    )
    inspect_result = WorkerResult.model_construct(
        status="success",
        summary="Inspected",
        files_changed=None,
        artifacts=None,
    )
    resumed_state = state.model_copy(
        update={
            "node_outcomes": [
                NodeOutcome.model_construct(
                    node_id="inspect",
                    status="completed",
                    result=inspect_result,
                    dependencies=[],
                    attempts=1,
                )
            ]
        }
    )

    result, outcomes, _ = asyncio.run(
        _await_decomposed_nodes(resumed_state, ContextAssertingWorker())
    )

    assert result.status == "success"
    assert [(outcome.node_id, outcome.status) for outcome in outcomes] == [
        ("inspect", "completed"),
        ("verify", "completed"),
    ]
