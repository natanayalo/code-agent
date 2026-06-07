"""Task-spec enrichment tests for the orchestrator brain."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from orchestrator.brain import RuleBasedOrchestratorBrain
from orchestrator.state import TaskRequest, TaskSpec
from tests.unit.orchestrator_brain_support import _ExplodingWorker, _SlowWorker, _StaticWorker
from workers import WorkerResult


def test_suggest_task_spec_returns_none_on_validation_failure() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(status="success", summary='{"accept_warning_status": true}')
        )
    )
    task_spec = TaskSpec(goal="g")
    suggestion = pytest.run if False else None
    suggestion = __import__("asyncio").run(
        brain.suggest_task_spec(
            task=TaskRequest(task_text="hello"),
            task_kind="implementation",
            task_plan=None,
            task_spec=task_spec,
        )
    )
    assert suggestion is None


@pytest.mark.asyncio
async def test_suggest_task_spec_model_backed_enrichment() -> None:
    suggestion_data = {
        "assumptions": ["Model assumption"],
        "acceptance_criteria": ["Model criteria"],
        "verification_commands": ["model-check"],
        "suggested_risk_level": "high",
        "rationale": "Model knows best",
    }

    worker = _StaticWorker(WorkerResult(status="success", summary=json.dumps(suggestion_data)))
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)

    task = TaskRequest(task_text="Urgent task", repo_url="url", branch="main")
    task_spec = TaskSpec(
        goal="goal", task_type="feature", risk_level="low", delivery_mode="workspace"
    )

    suggestion = await brain.suggest_task_spec(
        task=task, task_kind="implementation", task_plan=None, task_spec=task_spec
    )

    assert suggestion is not None
    assert suggestion.suggested_risk_level == "high"
    assert "Model assumption" in suggestion.assumptions
    assert "Model criteria" in suggestion.acceptance_criteria
    assert "model-check" in suggestion.verification_commands
    assert "[rules] rules_v1" in suggestion.rationale
    assert "[model] Model knows best" in suggestion.rationale


@pytest.mark.asyncio
async def test_suggest_task_spec_model_timeout_falls_back_to_rules() -> None:
    brain = RuleBasedOrchestratorBrain(planner_worker=_SlowWorker())
    from orchestrator import brain as brain_mod

    monkeypatch_timeout = 0.1
    with patch.object(brain_mod, "DEFAULT_TASK_SPEC_BRAIN_TIMEOUT_SECONDS", monkeypatch_timeout):
        task = TaskRequest(task_text="Urgent task")
        task_spec = TaskSpec(
            goal="goal", task_type="feature", risk_level="low", delivery_mode="workspace"
        )

        suggestion = await brain.suggest_task_spec(
            task=task, task_kind="implementation", task_plan=None, task_spec=task_spec
        )

        assert suggestion is not None
        assert suggestion.suggested_risk_level == "medium"
        assert suggestion.rationale == "rules_v1"


@pytest.mark.asyncio
async def test_suggest_task_spec_model_failure_falls_back_to_rules() -> None:
    brain = RuleBasedOrchestratorBrain(planner_worker=_ExplodingWorker())
    task = TaskRequest(task_text="Urgent task")
    task_spec = TaskSpec(
        goal="goal", task_type="feature", risk_level="low", delivery_mode="workspace"
    )
    suggestion = await brain.suggest_task_spec(
        task=task, task_kind="implementation", task_plan=None, task_spec=task_spec
    )
    assert suggestion is not None
    assert suggestion.suggested_risk_level == "medium"
    assert suggestion.rationale == "rules_v1"


@pytest.mark.asyncio
async def test_suggest_task_spec_risk_level_merging_takes_max() -> None:
    suggestion_data = {"suggested_risk_level": "low", "rationale": "Model thinks it is easy"}
    worker = _StaticWorker(WorkerResult(status="success", summary=json.dumps(suggestion_data)))
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)

    task = TaskRequest(task_text="Urgent task")
    task_spec = TaskSpec(
        goal="goal", task_type="feature", risk_level="low", delivery_mode="workspace"
    )

    suggestion = await brain.suggest_task_spec(
        task=task, task_kind="implementation", task_plan=None, task_spec=task_spec
    )

    assert suggestion.suggested_risk_level == "medium"
    expected_rationale = "[rules] rules_v1 | [model] Model thinks it is easy"
    assert expected_rationale in suggestion.rationale


@pytest.mark.asyncio
async def test_suggest_task_spec_model_non_success_falls_back_to_rules() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(WorkerResult(status="failure", summary="error"))
    )
    task = TaskRequest(task_text="Urgent task")
    task_spec = TaskSpec(
        goal="goal", task_type="feature", risk_level="low", delivery_mode="workspace"
    )
    suggestion = await brain.suggest_task_spec(
        task=task, task_kind="implementation", task_plan=None, task_spec=task_spec
    )
    assert suggestion is not None
    assert suggestion.suggested_risk_level == "medium"


@pytest.mark.asyncio
async def test_suggest_task_spec_model_non_success_sets_planner_span_outcome() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(
                status="failure",
                summary="planner auth not configured",
                failure_kind="provider_auth",
            )
        )
    )
    task = TaskRequest(task_text="Urgent task")
    task_spec = TaskSpec(
        goal="goal", task_type="feature", risk_level="low", delivery_mode="workspace"
    )
    with patch("orchestrator.brain.set_span_status_from_outcome") as set_status:
        suggestion = await brain.suggest_task_spec(
            task=task,
            task_kind="implementation",
            task_plan=None,
            task_spec=task_spec,
        )

    assert suggestion is not None
    set_status.assert_called_once()
    status_args, _ = set_status.call_args
    assert status_args[0] == "failure"
    assert "planner auth not configured" in status_args[1]


@pytest.mark.asyncio
async def test_suggest_task_spec_model_empty_summary_falls_back_to_rules() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(WorkerResult(status="success", summary=""))
    )
    task = TaskRequest(task_text="Urgent task")
    task_spec = TaskSpec(
        goal="goal", task_type="feature", risk_level="low", delivery_mode="workspace"
    )
    suggestion = await brain.suggest_task_spec(
        task=task, task_kind="implementation", task_plan=None, task_spec=task_spec
    )
    assert suggestion is not None
    assert suggestion.suggested_risk_level == "medium"


@pytest.mark.asyncio
async def test_suggest_task_spec_model_malformed_json_falls_back_to_rules() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(WorkerResult(status="success", summary="not-json"))
    )
    task = TaskRequest(task_text="Urgent task")
    task_spec = TaskSpec(
        goal="goal", task_type="feature", risk_level="low", delivery_mode="workspace"
    )
    suggestion = await brain.suggest_task_spec(
        task=task, task_kind="implementation", task_plan=None, task_spec=task_spec
    )
    assert suggestion is not None
    assert suggestion.suggested_risk_level == "medium"


def test_merge_list_ensures_strict_uniqueness() -> None:
    from orchestrator.brain import _merge_list

    a = ["a", "b", "a"]
    b = ["c", "b", "d"]
    merged = _merge_list(a, b)
    assert merged == ["a", "b", "c", "d"]


@pytest.mark.asyncio
async def test_suggest_task_spec_model_uses_empty_secrets() -> None:
    worker = _StaticWorker(WorkerResult(status="success", summary='{"assumptions":["test"]}'))
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)

    task = TaskRequest(task_text="text", secrets={"key": "secret"})
    task_spec = TaskSpec(goal="goal")

    await brain.suggest_task_spec(
        task=task, task_kind="implementation", task_plan=None, task_spec=task_spec
    )

    assert worker.requests[-1].secrets == {}
