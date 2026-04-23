"""Unit tests for the orchestrator review stage."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import orchestrator.review as review_module
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


@pytest.mark.anyio
async def test_review_result_resolves_windows_style_workspace_uri_for_prompt(monkeypatch):
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "add a feature"},
            "verification": {"status": "passed", "items": []},
            "result": {
                "status": "success",
                "summary": "added feature",
                "files_changed": ["feature.py"],
                "diff_text": "+++ feature.py\n+new line",
                "artifacts": [
                    {
                        "name": "workspace",
                        "uri": "file:///C:/repo/code-agent",
                        "artifact_type": "workspace_archive",
                    }
                ],
            },
            "dispatch": {"worker_type": "gemini"},
        }
    )

    captured_workspace_path: dict[str, Path] = {}

    def fake_build_review_prompt(
        *,
        workspace_path: Path,
        review_context_packet: str,  # noqa: ARG001
        reviewer_kind: str,  # noqa: ARG001
        task_text: str,  # noqa: ARG001
    ) -> str:
        captured_workspace_path["path"] = workspace_path
        return "prompt"

    monkeypatch.setattr(review_module, "build_review_prompt", fake_build_review_prompt)

    mock_reviewer = AsyncMock()
    review_payload = {
        "summary": "ok",
        "confidence": 1.0,
        "outcome": "no_findings",
        "findings": [],
    }
    mock_reviewer.run.return_value = WorkerResult(
        status="success",
        summary=("```json\n" f"{json.dumps(review_payload)}\n" "```"),
    )

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert captured_workspace_path["path"] == Path("C:/repo/code-agent")


@pytest.mark.anyio
async def test_review_result_logs_warning_when_workspace_path_missing(caplog):
    caplog.set_level("WARNING", logger="orchestrator.review")
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done", "artifacts": []},
            "dispatch": {"worker_type": "gemini"},
        }
    )

    mock_reviewer = AsyncMock()
    mock_reviewer.run.return_value = WorkerResult(
        status="success",
        summary='{"reviewer_kind":"independent_reviewer","summary":"ok","confidence":1.0,"outcome":"no_findings","findings":[]}',
    )

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert (
        "Independent review workspace path unavailable; falling back to current directory."
        in caplog.text
    )


@pytest.mark.anyio
async def test_review_result_logs_warning_when_using_same_worker_type(caplog):
    caplog.set_level("WARNING", logger="orchestrator.review")
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "codex"},
        }
    )

    mock_reviewer = AsyncMock()
    mock_reviewer.run.return_value = WorkerResult(
        status="success",
        summary='{"reviewer_kind":"independent_reviewer","summary":"ok","confidence":1.0,"outcome":"no_findings","findings":[]}',
    )

    res = await review_result(state, worker_factory={"codex": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert "Independent review is using the same worker type as execution (codex)." in caplog.text


@pytest.mark.anyio
async def test_review_result_logs_warnings_for_non_success_and_unparseable_output(caplog):
    caplog.set_level("WARNING", logger="orchestrator.review")
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "gemini"},
        }
    )

    mock_reviewer = AsyncMock()
    mock_reviewer.run.return_value = WorkerResult(status="error", summary="not-json")

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert "Independent review worker returned non-success status: error" in caplog.text
    assert "Independent review output could not be parsed into ReviewResult." in caplog.text


@pytest.mark.anyio
async def test_review_result_includes_fallback_session_state_in_review_context(monkeypatch):
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo task"},
            "normalized_task_text": "normalized demo task",
            "verification": {"status": "passed", "items": []},
            "result": {
                "status": "success",
                "summary": "done",
                "files_changed": ["a.py", "b.py"],
                "diff_text": "+++ a.py\n+print('x')",
            },
            "dispatch": {"worker_type": "gemini"},
        }
    )

    captured_session_state: dict[str, object] = {}

    def fake_pack_reviewer_context(
        *,
        task_text: str,  # noqa: ARG001
        worker_summary: str,  # noqa: ARG001
        files_changed: list[str],  # noqa: ARG001
        diff_text: str,  # noqa: ARG001
        commands_run: list[object],  # noqa: ARG001
        verifier_report: dict[str, object] | None,  # noqa: ARG001
        session_state: dict[str, object] | None,
        max_characters: int = 12000,  # noqa: ARG001
    ) -> str:
        captured_session_state["value"] = session_state
        return "ctx"

    monkeypatch.setattr(review_module, "pack_reviewer_context", fake_pack_reviewer_context)
    monkeypatch.setattr(
        review_module,
        "build_review_prompt",
        lambda **kwargs: "prompt",  # noqa: ARG005
    )

    mock_reviewer = AsyncMock()
    mock_reviewer.run.return_value = WorkerResult(
        status="success",
        summary='{"reviewer_kind":"independent_reviewer","summary":"ok","confidence":1.0,"outcome":"no_findings","findings":[]}',
    )

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert captured_session_state["value"] == {
        "active_goal": "normalized demo task",
        "files_touched": ["a.py", "b.py"],
    }


@pytest.mark.anyio
async def test_review_result_times_out_worker_run(monkeypatch, caplog):
    caplog.set_level("WARNING", logger="orchestrator.review")
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo task"},
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "gemini"},
        }
    )

    monkeypatch.setattr(review_module, "_resolve_review_timeout_seconds", lambda _state: 1)

    async def slow_run(*args, **kwargs):  # noqa: ANN002, ANN003
        await asyncio.sleep(2)
        return WorkerResult(status="success", summary="{}")

    mock_reviewer = AsyncMock()
    mock_reviewer.run.side_effect = slow_run

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert "Independent review pass timed out and was skipped." in caplog.text


@pytest.mark.anyio
async def test_review_result_propagates_cancellation():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo task"},
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "gemini"},
        }
    )

    mock_reviewer = AsyncMock()
    mock_reviewer.run.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await review_result(state, worker_factory={"gemini": mock_reviewer})
