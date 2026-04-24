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
    cancellation_observed: dict[str, bool] = {"value": False}

    async def slow_run(*args, **kwargs):  # noqa: ANN002, ANN003
        try:
            await asyncio.sleep(2)
            return WorkerResult(status="success", summary="{}")
        except asyncio.CancelledError:
            cancellation_observed["value"] = True
            raise

    mock_reviewer = AsyncMock()
    mock_reviewer.run.side_effect = slow_run

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert "Independent review pass timed out and was skipped." in caplog.text
    assert cancellation_observed["value"] is True


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


@pytest.mark.anyio
async def test_review_result_suppresses_style_and_low_confidence_findings_by_default():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "gemini"},
        }
    )
    mock_reviewer = AsyncMock()
    review_payload = {
        "summary": "Found issues",
        "confidence": 0.9,
        "outcome": "findings",
        "findings": [
            {
                "title": "Use clearer variable name",
                "category": "style",
                "confidence": 0.95,
                "file_path": "main.py",
                "severity": "low",
                "why_it_matters": "Readability",
            },
            {
                "title": "Potential branch bug",
                "category": "logic",
                "confidence": 0.4,
                "file_path": "main.py",
                "severity": "high",
                "why_it_matters": "Can break behavior",
            },
        ],
    }
    mock_reviewer.run.return_value = WorkerResult(
        status="success",
        summary=f"```json\n{json.dumps(review_payload)}\n```",
    )

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert res["review"]["outcome"] == "no_findings"
    assert res["review"]["findings"] == []
    assert len(res["review"]["suppressed_findings"]) == 2
    assert res["review"]["summary"].startswith(review_module.SUPPRESSED_FINDINGS_SUMMARY_PREFIX)


@pytest.mark.anyio
async def test_review_result_respects_configured_severity_and_style_overrides():
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "constraints": {
                    "independent_review_min_severity": "high",
                    "independent_review_include_style_findings": True,
                    "independent_review_min_confidence": 0.55,
                    "independent_review_min_confidence_by_severity": {
                        "critical": 0.9,
                        "high": 0.5,
                    },
                },
            },
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "gemini"},
        }
    )
    mock_reviewer = AsyncMock()
    review_payload = {
        "summary": "Found issues",
        "confidence": 0.9,
        "outcome": "findings",
        "findings": [
            {
                "title": "Style note",
                "category": "style",
                "confidence": 0.95,
                "file_path": "main.py",
                "severity": "low",
                "why_it_matters": "Readability",
            },
            {
                "title": "High severity bug",
                "category": "logic",
                "confidence": 0.7,
                "file_path": "main.py",
                "severity": "high",
                "why_it_matters": "Behavioral failure",
            },
            {
                "title": "Critical issue but low confidence",
                "category": "logic",
                "confidence": 0.7,
                "file_path": "main.py",
                "severity": "critical",
                "why_it_matters": "Potential outage",
            },
        ],
    }
    mock_reviewer.run.return_value = WorkerResult(
        status="success",
        summary=f"```json\n{json.dumps(review_payload)}\n```",
    )

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert res["review"]["outcome"] == "findings"
    assert [finding["title"] for finding in res["review"]["findings"]] == ["High severity bug"]
    assert len(res["review"]["suppressed_findings"]) == 2


@pytest.mark.anyio
async def test_review_result_uses_severity_threshold_over_global_by_default():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "gemini"},
        }
    )
    mock_reviewer = AsyncMock()
    review_payload = {
        "summary": "Found issues",
        "confidence": 0.9,
        "outcome": "findings",
        "findings": [
            {
                "title": "High severity finding under global threshold",
                "category": "logic",
                "confidence": 0.62,
                "file_path": "main.py",
                "severity": "high",
                "why_it_matters": "Behavioral correctness risk",
            }
        ],
    }
    mock_reviewer.run.return_value = WorkerResult(
        status="success",
        summary=f"```json\n{json.dumps(review_payload)}\n```",
    )

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert res["review"]["outcome"] == "findings"
    assert [finding["title"] for finding in res["review"]["findings"]] == [
        "High severity finding under global threshold"
    ]
    assert res["review"]["suppressed_findings"] == []


@pytest.mark.anyio
async def test_review_result_uses_explicit_global_confidence_as_baseline():
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "constraints": {
                    "independent_review_min_confidence": 0.85,
                    "independent_review_min_confidence_by_severity": {"high": 0.75},
                },
            },
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "gemini"},
        }
    )
    mock_reviewer = AsyncMock()
    review_payload = {
        "summary": "Found issues",
        "confidence": 0.9,
        "outcome": "findings",
        "findings": [
            {
                "title": "Medium below explicit global baseline",
                "category": "logic",
                "confidence": 0.8,
                "file_path": "main.py",
                "severity": "medium",
                "why_it_matters": "Could be risky",
            },
            {
                "title": "High allowed by explicit severity override",
                "category": "logic",
                "confidence": 0.8,
                "file_path": "main.py",
                "severity": "high",
                "why_it_matters": "Likely bug",
            },
        ],
    }
    mock_reviewer.run.return_value = WorkerResult(
        status="success",
        summary=f"```json\n{json.dumps(review_payload)}\n```",
    )

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert res["review"]["outcome"] == "findings"
    assert [finding["title"] for finding in res["review"]["findings"]] == [
        "High allowed by explicit severity override"
    ]
    assert len(res["review"]["suppressed_findings"]) == 1
    assert res["review"]["suppressed_findings"][0]["finding"]["title"] == (
        "Medium below explicit global baseline"
    )


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
            "dispatch": {"worker_type": "gemini"},
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

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

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
            "dispatch": {"worker_type": "gemini"},
        }
    )

    res = await review_result(state, worker_factory={"gemini": AsyncMock()})

    assert res["current_step"] == "review_result"
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
            "dispatch": {"worker_type": "gemini"},
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

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

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
            "dispatch": {"worker_type": "gemini"},
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

    res = await review_result(state, worker_factory={"gemini": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert res["repair_handoff_requested"] is True
    updated_constraints = res["task"]["constraints"]
    assert updated_constraints["independent_review_repair_passes_used"] == 1
    assert updated_constraints["skip_independent_review"] is False


def test_coerce_probability_returns_none_for_overflowing_numeric_input() -> None:
    assert review_module._coerce_probability(10**400) is None
