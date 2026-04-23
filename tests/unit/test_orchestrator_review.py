"""Unit tests for the orchestrator review stage."""

from unittest.mock import AsyncMock

import pytest

from orchestrator.review import review_result
from orchestrator.state import OrchestratorState
from workers import WorkerResult


@pytest.mark.anyio
async def test_review_result_skips_when_not_successful():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "verification": {"status": "failed", "items": []}}
    )

    res = await review_result(state)

    assert res["current_step"] == "review_result"
    assert "review" not in res


@pytest.mark.anyio
async def test_review_result_skips_when_explicitly_disabled():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo", "constraints": {"skip_independent_review": True}},
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
        }
    )

    res = await review_result(state)

    assert res["current_step"] == "review_result"
    assert "review" not in res


@pytest.mark.anyio
async def test_review_result_runs_and_parses_findings():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "add a feature"},
            "verification": {"status": "passed", "items": []},
            "result": {
                "status": "success",
                "summary": "added feature",
                "files_changed": ["feature.py"],
                "diff_text": "+++ feature.py\n+new line",
            },
            "dispatch": {"worker_type": "gemini"},
        }
    )

    # Mock worker
    mock_reviewer = AsyncMock()
    # Return a JSON payload that parse_review_result will accept
    review_payload = {
        "summary": "Review complete",
        "confidence": 0.9,
        "outcome": "findings",
        "findings": [
            {
                "title": "Missing docstring",
                "category": "documentation",
                "confidence": 0.8,
                "file_path": "feature.py",
                "line_start": 1,
                "line_end": 1,
                "severity": "low",
                "why_it_matters": "Documentation is good",
            }
        ],
    }

    import json

    mock_reviewer.run.return_value = WorkerResult(
        status="success", summary=f"Here is the review:\n```json\n{json.dumps(review_payload)}\n```"
    )

    worker_factory = {"gemini": mock_reviewer}

    res = await review_result(state, worker_factory=worker_factory)

    assert res["current_step"] == "review_result"
    assert res["review"]["outcome"] == "findings"
    assert res["review"]["findings"][0]["title"] == "Missing docstring"
    assert res["review"]["reviewer_kind"] == "independent_reviewer"

    # Verify worker was called with the right prompt kind
    args, kwargs = mock_reviewer.run.call_args
    assert "system_prompt" in kwargs
    assert "## Review Task" in kwargs["system_prompt"]
    assert "Reviewer kind: independent_reviewer" in kwargs["system_prompt"]


@pytest.mark.anyio
async def test_review_result_handles_worker_failure_gracefully():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "gemini"},
        }
    )

    mock_reviewer = AsyncMock()
    mock_reviewer.run.side_effect = Exception("API error")

    worker_factory = {"gemini": mock_reviewer}

    res = await review_result(state, worker_factory=worker_factory)

    assert res["current_step"] == "review_result"
    assert "review" not in res
