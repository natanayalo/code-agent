from datetime import timedelta

import pytest
from temporalio import workflow

from orchestrator.temporal.policy import activity_options
from orchestrator.temporal.workflows import (
    MAX_PERMISSION_ESCALATIONS,
    TaskExecutionWorkflow,
)


@pytest.fixture(autouse=True)
def _legacy_workflow_version_for_direct_unit_calls(monkeypatch) -> None:
    """Direct workflow-method tests have no Temporal runtime patch context."""
    monkeypatch.setattr(workflow, "patched", lambda _patch_id: False)


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


def test_node_execution_activity_can_use_profile_queue() -> None:
    """Only execution activities may leave the orchestration queue."""
    options = activity_options("run_decomposed_node", task_queue="code-agent-codex")

    assert options["task_queue"] == "code-agent-codex"
    with pytest.raises(ValueError, match="Only execution activities"):
        activity_options("merge_node_wave", task_queue="code-agent-codex")


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
async def test_decomposed_workflow_runs_one_node_wave_before_the_next_selection(
    monkeypatch,
) -> None:
    """New histories coordinate select, execute, and merge sequentially."""
    activity_names: list[str] = []
    selections = iter(
        [
            {
                "action": "execute",
                "execution_task_queue": "code-agent-codex",
                "activity_request": {
                    "task_id": "task-id",
                    "plan_id": "plan-id",
                    "node_id": "node-a",
                    "logical_attempt": 1,
                    "logical_activity_key": "node-activity:v1:plan-id:node-a:1",
                    "effective_input_digest": "a" * 64,
                },
            },
            {"action": "complete"},
        ]
    )

    async def execute_activity(name: str, *args, **kwargs):
        activity_names.append(name)
        if name == "classify_and_plan":
            return {}
        if name == "decompose_task":
            return {"execution_shape": "decomposed", "execution_task_queue": "code-agent-codex"}
        if name == "select_next_node":
            return next(selections)
        if name == "run_decomposed_node":
            return {
                "node_id": "node-a",
                "logical_activity_key": "node-activity:v1:plan-id:node-a:1",
                "status": "completed",
                "result_digest": "b" * 64,
                "continuation": "continue",
            }
        if name == "merge_node_wave":
            return {"continuation": "continue"}
        return {}

    monkeypatch.setattr(workflow, "patched", lambda _patch_id: True)
    monkeypatch.setattr(workflow, "execute_activity", execute_activity)

    result = await TaskExecutionWorkflow()._run_lifecycle("task-id")

    assert result["status"] == "completed"
    first_selection = activity_names.index("select_next_node")
    merge = activity_names.index("merge_node_wave")
    assert first_selection < activity_names.index("run_decomposed_node") < merge
    assert merge < activity_names.index("select_next_node", first_selection + 1)
    assert "run_worker" not in activity_names


@pytest.mark.anyio
async def test_v2_fanout_starts_both_nodes_before_ordered_merge(monkeypatch) -> None:
    """A V2 selection schedules siblings together and preserves selection order."""
    started: list[str] = []
    merges: list[dict] = []

    def item(node_id: str, digest: str) -> dict:
        return {
            "node_id": node_id,
            "execution_task_queue": "code-agent-codex",
            "activity_request": {
                "task_id": "task-id",
                "plan_id": "plan",
                "node_id": node_id,
                "logical_attempt": 1,
                "logical_activity_key": f"node-activity:v1:plan:{node_id}:1",
                "effective_input_digest": digest * 64,
            },
        }

    selections = iter(
        [
            {
                "schema_version": 2,
                "action": "execute_wave",
                "fanout_applied": True,
                "wave_id": "wave",
                "items": [
                    item("a", "a"),
                    item("b", "b"),
                ],
            },
            {"action": "complete"},
        ]
    )

    async def execute_activity(name: str, *args, **kwargs):
        if name == "classify_and_plan":
            return {}
        if name == "decompose_task":
            return {"execution_shape": "decomposed"}
        if name == "select_next_node":
            return next(selections)
        if name == "run_decomposed_node":
            request = kwargs["args"][1]
            started.append(request["node_id"])
            return {
                "node_id": request["node_id"],
                "logical_activity_key": request["logical_activity_key"],
                "status": "completed",
                "result_digest": request["effective_input_digest"],
                "continuation": "continue",
            }
        if name == "merge_node_wave":
            merges.append(kwargs["args"][1])
            return {"continuation": "continue"}
        return {}

    monkeypatch.setattr(workflow, "patched", lambda _patch_id: True)
    monkeypatch.setattr(workflow, "execute_activity", execute_activity)

    result = await TaskExecutionWorkflow()._run_lifecycle("task-id")

    assert result["status"] == "completed"
    assert started == ["a", "b"]
    assert len(merges) == 1
    assert [item["node_id"] for item in merges[0]["selection"]["items"]] == ["a", "b"]


@pytest.mark.anyio
async def test_decomposed_workflow_bounds_permission_escalations(monkeypatch) -> None:
    """A blocked node cannot bypass the task-level escalation cap."""
    workflow_instance = TaskExecutionWorkflow()
    activity_names: list[str] = []

    async def execute_activity(name: str, *args, **kwargs):
        activity_names.append(name)
        if name == "select_next_node":
            return {"action": "await_permission", "node_id": "blocked-node"}
        return {"execution_shape": "decomposed"} if name == "decompose_task" else {}

    async def wait_condition(_predicate) -> None:
        workflow_instance.permission_escalation_decision = True

    monkeypatch.setattr(workflow, "patched", lambda _patch_id: True)
    monkeypatch.setattr(workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(workflow, "wait_condition", wait_condition)

    result = await workflow_instance._run_lifecycle("task-id")

    assert result["status"] == "failed"
    assert activity_names.count("request_permission_escalation") == MAX_PERMISSION_ESCALATIONS
    assert activity_names[-2:] == ["fail_node_permission_escalation", "record_workflow_failure"]


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
