"""Unit tests for additional functions in orchestrator/graph.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.graph import (
    _await_decomposed_nodes,
    _cancelled_worker_result,
    _consume_worker_task_result,
    _memory_admission_evidence,
    _memory_candidate_from_entry,
    _persist_memory_entry,
    _settle_cancelled_worker_task,
    _timed_out_worker_result,
    _unexpected_worker_error_result,
    persist_memory,
)
from orchestrator.state import (
    DecomposedTaskNode,
    DecomposedTaskPlan,
    OrchestratorState,
    PersistMemoryEntry,
    TaskSpec,
)
from workers import WorkerCommand, WorkerResult


@pytest.mark.asyncio
async def test_await_decomposed_nodes():
    node1 = DecomposedTaskNode(
        node_id="n1",
        title="Node 1",
        node_kind="implement",
        depends_on=[],
        task_spec=TaskSpec(goal="Goal 1", acceptance_criteria=["AC1"], task_type="feature"),
        max_attempts=1,
    )
    node2 = DecomposedTaskNode(
        node_id="n2",
        title="Node 2",
        node_kind="implement",
        depends_on=["n1"],
        task_spec=TaskSpec(goal="Goal 2", acceptance_criteria=["AC2"], task_type="feature"),
        max_attempts=1,
    )
    plan = DecomposedTaskPlan(status="decomposed", nodes=[node1, node2])

    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        decomposed_plan=plan,
    )

    worker = MagicMock()
    worker.run = AsyncMock()
    worker.run.return_value = WorkerResult(status="success", summary="node 1 done")

    # Both succeed
    agg_res, outcomes, manifest = await _await_decomposed_nodes(state, worker)
    assert len(outcomes) == 2
    assert outcomes[0].status == "completed"
    assert outcomes[1].status == "completed"

    # Dependency failure leads to skipped second node
    worker.run.side_effect = [
        WorkerResult(status="failure", summary="node 1 failed"),
        WorkerResult(status="success"),
    ]
    state_fail = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        decomposed_plan=plan,
    )
    agg_res2, outcomes2, _ = await _await_decomposed_nodes(state_fail, worker)
    assert len(outcomes2) == 2
    assert outcomes2[0].status == "failed"
    assert outcomes2[1].status == "skipped"


def test_timed_out_and_cancelled_results():
    r1 = _timed_out_worker_result(30)
    assert r1.status == "failure" and r1.failure_kind == "timeout"

    r2 = _cancelled_worker_result()
    assert r2.status == "failure" and r2.failure_kind == "timeout"

    r3 = _unexpected_worker_error_result(RuntimeError("Crashed"))
    assert r3.status == "error" and "Crashed" in r3.summary


@pytest.mark.asyncio
async def test_consume_worker_task_result():
    async def _f():
        return WorkerResult(status="success")

    t = asyncio.create_task(_f())
    await t
    _consume_worker_task_result(t, worker_type="codex", session_id="s1")


@pytest.mark.asyncio
async def test_settle_cancelled_worker_task():
    async def _slow():
        await asyncio.sleep(10)
        return WorkerResult(status="success")

    t = asyncio.create_task(_slow())
    res = await _settle_cancelled_worker_task(
        t, worker_type="codex", session_id="s1", grace_period_seconds=1
    )
    assert res is None


def test_persist_memory():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        memory_to_persist=[
            PersistMemoryEntry(category="personal", memory_key="k1", value={"v": 1})
        ],
    )
    res = persist_memory(state)
    assert res["current_step"] == "persist_memory"
    assert len(res["memory_to_persist"]) == 1


def test_persist_memory_entry_helpers():
    db_session = MagicMock()
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "https://github.com/org/repo"})

    # personal entry
    e_pers = PersistMemoryEntry(category="personal", memory_key="k1", value={"v": 1})
    with patch("orchestrator.graph.PersonalMemoryRepository") as _mock_repo:
        assert _persist_memory_entry(db_session, state, e_pers) is True

    # project entry with repo_url
    e_proj = PersistMemoryEntry(
        category="project", memory_key="k2", value={"v": 2}, repo_url="https://github.com/org/repo"
    )
    with patch("orchestrator.graph.ProjectMemoryRepository") as _mock_repo:
        assert _persist_memory_entry(db_session, state, e_proj) is True

    # project entry without repo_url
    state_no_repo = OrchestratorState(task={"task_text": "txt"})
    e_proj_no = PersistMemoryEntry(category="project", memory_key="k3", value={"v": 3})
    assert _persist_memory_entry(db_session, state_no_repo, e_proj_no) is False


def test_memory_admission_evidence_and_candidate():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(
            status="success",
            commands_run=[WorkerCommand(command="pytest", exit_code=0)],
            files_changed=["src/app.py"],
        ),
    )
    ev = _memory_admission_evidence(state)
    assert len(ev) == 2

    entry = PersistMemoryEntry(category="project", memory_key="k1", value={"v": 1})
    cand = _memory_candidate_from_entry(state, entry, ev)
    assert cand.memory_key == "k1"
