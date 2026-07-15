from datetime import timedelta

import pytest
from temporalio import workflow

from orchestrator.temporal.policy import activity_options
from orchestrator.temporal.workflows import MAX_PERMISSION_ESCALATIONS, TaskExecutionWorkflow


def test_worker_activity_policy_has_bounded_retry_and_heartbeat() -> None:
    """Long-running worker work must retain explicit recovery bounds."""
    options = activity_options("run_worker", task_queue="code-agent-codex")

    assert options["start_to_close_timeout"] == timedelta(minutes=30)
    assert options["heartbeat_timeout"] == timedelta(seconds=20)
    assert options["task_queue"] == "code-agent-codex"
    retry_policy = options["retry_policy"]
    assert retry_policy.maximum_attempts == 2
    assert retry_policy.initial_interval == timedelta(seconds=5)


def test_projection_failure_policy_is_bounded_and_does_not_use_a_worker_queue() -> None:
    """Terminal failure projection is retried on the orchestration queue only."""
    options = activity_options("record_workflow_failure")

    assert options["start_to_close_timeout"] == timedelta(minutes=5)
    assert "heartbeat_timeout" not in options
    assert "task_queue" not in options
    retry_policy = options["retry_policy"]
    assert retry_policy.maximum_attempts == 3


def test_unknown_activity_policy_is_rejected() -> None:
    """A new workflow activity must declare its lifecycle policy explicitly."""
    try:
        activity_options("unknown")
    except ValueError as exc:
        assert str(exc) == "Unknown Temporal activity policy: unknown"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Unknown activity policy unexpectedly resolved.")


@pytest.mark.anyio
async def test_workflow_persists_memory_before_terminal_delivery(monkeypatch) -> None:
    """The final worker state must still exist when memory persistence runs."""
    activity_names: list[str] = []

    async def execute_activity(name: str, *args, **kwargs):
        activity_names.append(name)
        if name == "classify_and_plan":
            return {}
        if name == "run_worker":
            return {}
        return None

    monkeypatch.setattr(workflow, "execute_activity", execute_activity)

    await TaskExecutionWorkflow()._run_lifecycle("task-id")

    assert activity_names[-2:] == ["persist_memory", "deliver_result"]


@pytest.mark.anyio
async def test_workflow_repeats_sequential_permission_escalations(monkeypatch) -> None:
    """Each worker retry must be able to request a fresh permission decision."""
    workflow_instance = TaskExecutionWorkflow()
    worker_results = iter(
        [
            {"requires_permission_escalation": True},
            {"requires_permission_escalation": True},
            {"requires_permission_escalation": False},
        ]
    )
    activity_names: list[str] = []

    async def execute_activity(name: str, *args, **kwargs):
        activity_names.append(name)
        return next(worker_results) if name == "run_worker" else {}

    async def wait_condition(predicate) -> None:
        workflow_instance.permission_escalation_decision = True

    monkeypatch.setattr(workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(workflow, "wait_condition", wait_condition)

    result = await workflow_instance._run_lifecycle("task-id")

    assert result["status"] == "completed"
    assert activity_names.count("request_permission_escalation") == 2
    assert activity_names.count("resolve_permission_escalation") == 2
    assert activity_names.count("provision_workspace") == 3


@pytest.mark.anyio
async def test_permission_signal_during_request_activity_is_preserved(monkeypatch) -> None:
    """An early operator signal must survive request activity completion."""
    workflow_instance = TaskExecutionWorkflow()
    activity_names: list[str] = []

    async def execute_activity(name: str, *args, **kwargs):
        activity_names.append(name)
        if name == "request_permission_escalation":
            workflow_instance.permission_escalation_decision = True
        return None

    async def wait_condition(predicate) -> None:
        assert predicate()

    monkeypatch.setattr(workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(workflow, "wait_condition", wait_condition)

    assert await workflow_instance._handle_permission_escalation("task-id") is True
    assert activity_names == [
        "request_permission_escalation",
        "resolve_permission_escalation",
    ]


@pytest.mark.anyio
async def test_workflow_bounds_repeated_permission_escalations(monkeypatch) -> None:
    """A misconfigured worker must not loop on permission requests forever."""
    workflow_instance = TaskExecutionWorkflow()
    activity_names: list[str] = []

    async def execute_activity(name: str, *args, **kwargs):
        activity_names.append(name)
        if name == "classify_and_plan":
            return {}
        if name == "run_worker":
            return {"requires_permission_escalation": True}
        return None

    async def wait_condition(predicate) -> None:
        workflow_instance.permission_escalation_decision = True

    monkeypatch.setattr(workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(workflow, "wait_condition", wait_condition)

    result = await workflow_instance._run_lifecycle("task-id")

    assert result == {
        "status": "failed",
        "summary": (
            "Maximum sequential permission escalation limit reached "
            f"({MAX_PERMISSION_ESCALATIONS})."
        ),
    }
    assert activity_names.count("run_worker") == MAX_PERMISSION_ESCALATIONS + 1
    assert activity_names[-1] == "record_workflow_failure"
