# ruff: noqa: F403, F405
"""Repair-handoff orchestrator review unit tests."""

from __future__ import annotations

from tests.unit.orchestrator_review_support import *  # noqa: F403


@pytest.mark.anyio
async def test_review_result_requests_single_repair_handoff_for_actionable_findings():
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "constraints": {"independent_review_enable_repair_handoff": True},
            },
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "antigravity"},
        }
    )
    mock_reviewer = AsyncMock()
    review_payload = {
        "summary": "Found issues",
        "confidence": 0.9,
        "outcome": "findings",
        "findings": [
            {
                "title": "High severity bug",
                "category": "logic",
                "confidence": 0.92,
                "file_path": "main.py",
                "line_start": 10,
                "severity": "high",
                "why_it_matters": "Behavior can break",
            }
        ],
    }
    mock_reviewer.run.return_value = WorkerResult(
        status="success",
        summary=f"```json\n{json.dumps(review_payload)}\n```",
    )

    res = await review_result(state, worker_factory={"antigravity": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert res["repair_handoff_requested"] is True
    assert res["review"]["outcome"] == "findings"
    assert res["review"]["findings"][0]["title"] == "High severity bug"
    assert res["verification"] is None
    updated_constraints = res["task"]["constraints"]
    assert updated_constraints["independent_review_repair_passes_used"] == 1
    assert updated_constraints["skip_independent_review"] is True
    assert "independent_review_repair_request" in updated_constraints


@pytest.mark.anyio
async def test_review_result_does_not_request_handoff_when_explicitly_disabled():
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "constraints": {"independent_review_enable_repair_handoff": False},
            },
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "antigravity"},
        }
    )
    mock_reviewer = AsyncMock()
    mock_reviewer.run.return_value = WorkerResult(
        status="success",
        summary=json.dumps(
            {
                "summary": "Found issues",
                "confidence": 0.9,
                "outcome": "findings",
                "findings": [
                    {
                        "title": "High severity bug",
                        "category": "logic",
                        "confidence": 0.92,
                        "file_path": "main.py",
                        "line_start": 10,
                        "severity": "high",
                        "why_it_matters": "Behavior can break",
                    }
                ],
            }
        ),
    )
    res = await review_result(state, worker_factory={"antigravity": mock_reviewer})
    assert "repair_handoff_requested" not in res


@pytest.mark.anyio
async def test_review_result_cleans_repair_handoff_constraints_after_repair_pass():
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "constraints": {
                    "independent_review_repair_request": "repair text",
                    "skip_independent_review": True,
                    "independent_review_repair_passes_used": 1,
                },
            },
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "antigravity"},
        }
    )

    res = await review_result(state, worker_factory={"antigravity": AsyncMock()})

    assert res["current_step"] == "review_result"
    assert res["review"] is None
    assert "independent_review_repair_request" not in res["task"]["constraints"]
    assert "skip_independent_review" not in res["task"]["constraints"]
    assert res["task"]["constraints"]["independent_review_repair_passes_used"] == 1


@pytest.mark.anyio
async def test_review_result_respects_max_repair_pass_budget():
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "constraints": {
                    "independent_review_enable_repair_handoff": True,
                    "independent_review_max_repair_passes": 1,
                    "independent_review_repair_passes_used": 1,
                },
            },
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "antigravity"},
        }
    )
    mock_reviewer = AsyncMock()
    review_payload = {
        "summary": "Found issues",
        "confidence": 0.9,
        "outcome": "findings",
        "findings": [
            {
                "title": "High severity bug",
                "category": "logic",
                "confidence": 0.92,
                "file_path": "main.py",
                "severity": "high",
                "why_it_matters": "Behavior can break",
            }
        ],
    }
    mock_reviewer.run.return_value = WorkerResult(
        status="success",
        summary=f"```json\n{json.dumps(review_payload)}\n```",
    )

    res = await review_result(state, worker_factory={"antigravity": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert "repair_handoff_requested" not in res
    assert res["review"]["outcome"] == "findings"


@pytest.mark.anyio
async def test_review_result_keeps_review_enabled_when_repair_budget_not_exhausted():
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "constraints": {
                    "independent_review_enable_repair_handoff": True,
                    "independent_review_max_repair_passes": 2,
                    "independent_review_repair_passes_used": 0,
                },
            },
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "antigravity"},
        }
    )
    mock_reviewer = AsyncMock()
    review_payload = {
        "summary": "Found issues",
        "confidence": 0.9,
        "outcome": "findings",
        "findings": [
            {
                "title": "Another high severity bug",
                "category": "logic",
                "confidence": 0.9,
                "file_path": "main.py",
                "severity": "high",
                "why_it_matters": "Behavior can break",
            }
        ],
    }
    mock_reviewer.run.return_value = WorkerResult(
        status="success",
        summary=f"```json\n{json.dumps(review_payload)}\n```",
    )

    res = await review_result(state, worker_factory={"antigravity": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert res["repair_handoff_requested"] is True
    updated_constraints = res["task"]["constraints"]
    assert updated_constraints["independent_review_repair_passes_used"] == 1
    assert updated_constraints["skip_independent_review"] is False
