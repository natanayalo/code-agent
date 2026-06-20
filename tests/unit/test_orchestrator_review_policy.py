# ruff: noqa: F403, F405
"""Finding-policy orchestrator review unit tests."""

from __future__ import annotations

from tests.unit.orchestrator_review_support import *  # noqa: F403


@pytest.mark.anyio
async def test_review_result_suppresses_style_and_low_confidence_findings_by_default():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done", "files_changed": ["main.py"]},
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

    res = await review_result(state, worker_factory={"antigravity": mock_reviewer})

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
            "result": {"status": "success", "summary": "done", "files_changed": ["main.py"]},
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

    res = await review_result(state, worker_factory={"antigravity": mock_reviewer})

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
            "result": {"status": "success", "summary": "done", "files_changed": ["main.py"]},
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

    res = await review_result(state, worker_factory={"antigravity": mock_reviewer})

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
            "result": {"status": "success", "summary": "done", "files_changed": ["main.py"]},
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

    res = await review_result(state, worker_factory={"antigravity": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert res["review"]["outcome"] == "findings"
    assert [finding["title"] for finding in res["review"]["findings"]] == [
        "High allowed by explicit severity override"
    ]
    assert len(res["review"]["suppressed_findings"]) == 1
    assert res["review"]["suppressed_findings"][0]["finding"]["title"] == (
        "Medium below explicit global baseline"
    )


def test_coerce_probability_returns_none_for_overflowing_numeric_input() -> None:
    assert review_module._coerce_probability(10**400) is None


@pytest.mark.anyio
async def test_review_result_skips_on_read_only_or_no_files_changed():
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "constraints": {"read_only": True},
            },
            "verification": {"status": "passed", "items": []},
            "result": {"status": "success", "summary": "done", "files_changed": []},
            "dispatch": {"worker_type": "antigravity"},
        }
    )
    mock_reviewer = AsyncMock()

    res = await review_result(state, worker_factory={"antigravity": mock_reviewer})

    assert res["current_step"] == "review_result"
    assert res["review"] is None
    assert any("independent code-change review skipped" in msg for msg in res["progress_updates"])
    mock_reviewer.run.assert_not_called()
