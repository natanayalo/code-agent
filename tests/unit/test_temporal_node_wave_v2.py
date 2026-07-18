"""Regression coverage for M25.2 Temporal fan-out selection and merge behavior."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.base import Base
from db.enums import ExecutionPlanNodeStatus
from orchestrator.node_execution import NodeActivityResultRef
from orchestrator.state import NodeOutcome, OrchestratorState, WorkerDispatch
from orchestrator.temporal.activities import TaskExecutionActivities
from repositories import (
    ExecutionPlanRepository,
    TaskRepository,
    TemporalTaskStateRepository,
    session_scope,
)
from workers import WorkerProfile, WorkerResult


@dataclass
class _WaveFixture:
    session_factory: sessionmaker[Session]
    task_id: str
    plan_id: str
    state: OrchestratorState
    activity: TaskExecutionActivities


def _node_data(node_id: str, *, parallel_safe: bool = True) -> dict[str, object]:
    return {
        "node_id": node_id,
        "title": node_id.title(),
        "task_spec": {"goal": f"Inspect {node_id}"},
        "node_kind": "inspect" if node_id == "first" else "verify",
        "aggregation_role": "context" if node_id == "first" else "validation",
        "execution_mode": "read_only",
        "parallel_safe": parallel_safe,
        "max_attempts": 2,
    }


def _build_fixture(*, second_parallel_safe: bool = True) -> _WaveFixture:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_scope(factory) as session:
        task = TaskRepository(session).create(
            session_id="session",
            task_text="Inspect the repository",
            constraints={"read_only": True},
        )
        plan = ExecutionPlanRepository(session).create(task_id=task.id)
        for index, node in enumerate(
            [_node_data("first"), _node_data("second", parallel_safe=second_parallel_safe)]
        ):
            ExecutionPlanRepository(session).add_node(
                plan_id=plan.id,
                node_id=str(node["node_id"]),
                goal=str(node["title"]),
                sequence_number=index,
                task_spec=dict(node["task_spec"]),
                node_kind=str(node["node_kind"]),
                aggregation_role=str(node["aggregation_role"]),
                execution_mode=str(node["execution_mode"]),
                parallel_safe=bool(node["parallel_safe"]),
            )
        state = OrchestratorState.model_validate(
            {
                "task": {
                    "task_id": task.id,
                    "task_text": task.task_text,
                    "constraints": {"read_only": True},
                },
                "route": {
                    "chosen_worker": "codex",
                    "chosen_profile": "readonly-profile",
                    "runtime_mode": "native_agent",
                },
                "decomposed_plan": {
                    "triggered": True,
                    "status": "decomposed",
                    "nodes": [
                        _node_data("first"),
                        _node_data("second", parallel_safe=second_parallel_safe),
                    ],
                },
            }
        )
        TemporalTaskStateRepository(session).upsert(
            task_id=task.id, state=state.model_dump(mode="json")
        )

    async def _run_blocking(function, *args, **kwargs):
        return function(*args, **kwargs)

    activity = object.__new__(TaskExecutionActivities)
    activity.service = SimpleNamespace(
        session_factory=factory,
        _run_blocking=_run_blocking,
        decomposed_fanout_enabled=True,
        worker_profiles={
            "readonly-profile": WorkerProfile(
                name="readonly-profile",
                worker_type="codex",
                runtime_mode="native_agent",
                mutation_policy="read_only",
            )
        },
    )
    activity._get_current_state = lambda _task_id: state
    activity._load_task_trace_context = lambda _task_id: {}
    return _WaveFixture(
        session_factory=factory,
        task_id=task.id,
        plan_id=plan.id,
        state=state,
        activity=activity,
    )


def _result_ref(item: dict[str, object], *, status: str, digest: str) -> dict[str, object]:
    request = dict(item["activity_request"])
    return NodeActivityResultRef(
        node_id=str(item["node_id"]),
        logical_activity_key=str(request["logical_activity_key"]),
        status=status,
        result_digest=digest,
        continuation={
            "completed": "continue",
            "blocked": "await_permission",
            "failed": "retry_node",
        }[status],
    ).model_dump(mode="json")


def _persist_terminal_result(
    fixture: _WaveFixture,
    item: dict[str, object],
    *,
    status: str,
    failure_kind: str | None = None,
) -> dict[str, object]:
    request = dict(item["activity_request"])
    node_id = str(item["node_id"])
    key = str(request["logical_activity_key"])
    digest = ("a" if node_id == "first" else "b") * 64
    worker_result = WorkerResult(
        status="success" if status == "completed" else "failure",
        summary=f"{node_id} {status}",
        failure_kind=failure_kind,
        requested_permission="workspace_write" if status == "blocked" else None,
    )
    outcome = NodeOutcome(
        node_id=node_id,
        status=status,
        result=worker_result,
        attempts=1,
        logical_activity_key=key,
        result_digest=digest,
    )
    payload = {
        "worker_result": worker_result.model_dump(mode="json"),
        "node_outcome": outcome.model_dump(mode="json"),
    }
    plan_status = {
        "completed": ExecutionPlanNodeStatus.COMPLETED,
        "blocked": ExecutionPlanNodeStatus.BLOCKED,
    }.get(status, ExecutionPlanNodeStatus.FAILED)
    with session_scope(fixture.session_factory) as session:
        ExecutionPlanRepository(session).update_node(
            plan_id=fixture.plan_id,
            node_id=node_id,
            status=plan_status,
            latest_logical_activity_key=key,
            terminal_result_digest=digest,
            terminal_result_payload=payload,
        )
    return _result_ref(item, status=status, digest=digest)


@pytest.mark.anyio
async def test_select_next_node_returns_ordered_v2_wave_for_two_eligible_nodes() -> None:
    fixture = _build_fixture()

    selection = await fixture.activity.select_next_node_v2(fixture.task_id)

    assert selection["schema_version"] == 2
    assert selection["fanout_applied"] is True
    assert [item["node_id"] for item in selection["items"]] == ["first", "second"]
    assert selection["wave_id"].startswith(f"node-wave:v2:{fixture.plan_id}:")
    assert {item["activity_request"]["execution_capacity_key"] for item in selection["items"]} == {
        item["execution_task_queue"] for item in selection["items"]
    }


@pytest.mark.anyio
async def test_legacy_selection_does_not_acquire_a_fanout_capacity_permit() -> None:
    fixture = _build_fixture()

    selection = await fixture.activity.select_next_node(fixture.task_id)

    assert selection["action"] == "execute"
    assert selection["activity_request"]["execution_capacity_key"] is None


@pytest.mark.anyio
async def test_v2_selection_uses_the_effective_dispatch_profile_for_safety() -> None:
    fixture = _build_fixture()
    fixture.activity.service.worker_profiles["mutable-profile"] = WorkerProfile(
        name="mutable-profile",
        worker_type="codex",
        runtime_mode="native_agent",
        mutation_policy="patch_allowed",
    )
    dispatch_state = fixture.state.model_copy(
        update={"dispatch": WorkerDispatch(worker_profile="mutable-profile")}
    )
    fixture.activity._get_current_state = lambda _task_id: dispatch_state

    selection = await fixture.activity.select_next_node_v2(fixture.task_id)

    assert selection["action"] == "execute"
    assert selection["activity_request"]["execution_capacity_key"] is None


@pytest.mark.anyio
async def test_select_next_node_does_not_overtake_an_ineligible_second_node() -> None:
    fixture = _build_fixture(second_parallel_safe=False)

    selection = await fixture.activity.select_next_node_v2(fixture.task_id)

    assert selection["action"] == "execute"
    assert selection["node_id"] == "first"


@pytest.mark.parametrize(
    ("statuses", "failure_kinds", "continuation"),
    [
        (("completed", "completed"), (None, None), "continue"),
        (("completed", "failed"), (None, "worker_failure"), "retry_node"),
        (("completed", "blocked"), (None, None), "await_permission"),
        (("completed", "failed"), (None, "read_only_violation"), "fail_task"),
    ],
)
@pytest.mark.anyio
async def test_merge_v2_wave_projects_ordered_multi_result_outcomes(
    statuses: tuple[str, str],
    failure_kinds: tuple[str | None, str | None],
    continuation: str,
) -> None:
    fixture = _build_fixture()
    selection = await fixture.activity.select_next_node_v2(fixture.task_id)
    refs = [
        _persist_terminal_result(
            fixture,
            item,
            status=status,
            failure_kind=failure_kind,
        )
        for item, status, failure_kind in zip(
            selection["items"], statuses, failure_kinds, strict=True
        )
    ]

    result = fixture.activity._merge_v2_wave(fixture.task_id, selection, refs)

    assert result["continuation"] == continuation
    with session_scope(fixture.session_factory) as session:
        snapshot = TemporalTaskStateRepository(session).get(task_id=fixture.task_id)
        assert snapshot is not None
        merged = OrchestratorState.model_validate(snapshot.state)
        assert [outcome.node_id for outcome in merged.node_outcomes] == ["first", "second"]
        second = ExecutionPlanRepository(session).get_node(fixture.plan_id, "second")
        assert second is not None
        if continuation == "retry_node":
            assert second.status == ExecutionPlanNodeStatus.PENDING
            assert second.retry_count == 1
        if continuation in {"await_permission", "fail_task"}:
            assert merged.fanout_disabled_for_remainder is True


@pytest.mark.parametrize(
    "statuses",
    [("blocked", "failed"), ("failed", "blocked")],
)
@pytest.mark.anyio
async def test_merge_v2_wave_prioritizes_permission_over_retryable_failure(
    statuses: tuple[str, str],
) -> None:
    """A retryable sibling cannot bypass the earlier/later blocked-node HITL flow."""
    fixture = _build_fixture()
    selection = await fixture.activity.select_next_node_v2(fixture.task_id)
    refs = [
        _persist_terminal_result(fixture, item, status=status, failure_kind="worker_failure")
        for item, status in zip(selection["items"], statuses, strict=True)
    ]

    result = fixture.activity._merge_v2_wave(fixture.task_id, selection, refs)

    assert result["continuation"] == "await_permission"
    assert result["blocked_node_id"] == ("first" if statuses[0] == "blocked" else "second")


@pytest.mark.anyio
async def test_merge_v2_wave_keeps_terminal_sibling_when_other_evidence_is_missing() -> None:
    """An Activity exception reconciles committed evidence before failing the parent."""
    fixture = _build_fixture()
    selection = await fixture.activity.select_next_node_v2(fixture.task_id)
    first_ref = _persist_terminal_result(fixture, selection["items"][0], status="completed")

    result = fixture.activity._merge_v2_wave(
        fixture.task_id,
        selection,
        [first_ref, None],
    )

    assert result["continuation"] == "fail_task"
    with session_scope(fixture.session_factory) as session:
        snapshot = TemporalTaskStateRepository(session).get(task_id=fixture.task_id)
        assert snapshot is not None
        merged = OrchestratorState.model_validate(snapshot.state)
        assert [outcome.node_id for outcome in merged.node_outcomes] == ["first", "second"]
        assert merged.node_outcomes[0].status == "completed"
        assert merged.node_outcomes[1].result.failure_kind == "sandbox_infra"
