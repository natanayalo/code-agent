"""Improvement proposal scoring tests for the orchestrator brain."""

from __future__ import annotations

import json

import pytest

from orchestrator.brain import RuleBasedOrchestratorBrain
from orchestrator.improvement_suggestions import ImprovementSuggestionScoringContext
from orchestrator.reflection import FrictionReport, ImprovementSuggestion
from tests.unit.orchestrator_brain_support import _ExplodingWorker, _StaticWorker
from workers import Worker, WorkerRequest, WorkerResult


class _NoneWorker(Worker):
    """Worker double that simulates a buggy planner returning None."""

    async def run(
        self,
        request: WorkerRequest,
        *,
        system_prompt: str | None = None,
    ) -> WorkerResult:
        del request, system_prompt
        return None  # type: ignore[return-value]


def _report() -> FrictionReport:
    return FrictionReport(
        task_id="task-1",
        source="sandbox",
        description="Infra crash prevented checkout.",
        impact="blocked",
        context={"failure_kind": "sandbox_infra"},
    )


def _suggestion() -> ImprovementSuggestion:
    return ImprovementSuggestion(
        title="Harden sandbox infrastructure recovery",
        description="Reduce recurring sandbox friction observed during task execution.",
        value="high",
        effort="large",
        risk="high",
        layer_impact="sandbox",
        validation_path="Run sandbox runner integration tests.",
        hitl_need="required",
    )


def _context() -> ImprovementSuggestionScoringContext:
    return ImprovementSuggestionScoringContext(
        task_id="task-1",
        task_text="fix sandbox crash",
        repo_url="https://example.com/repo.git",
        branch="main",
        attempt_count=2,
        failure_kind="sandbox_infra",
        retry_context=True,
        session_id="session-1",
        task_constraints={"budget": "bounded"},
        task_budget={"worker_timeout_seconds": 30},
    )


@pytest.mark.asyncio
async def test_score_improvement_suggestion_model_success() -> None:
    payload = {
        "value": "high",
        "effort": "medium",
        "risk": "medium",
        "layer_impact": "orchestrator",
        "validation_path": "Run orchestrator persistence tests and vertical e2e smoke.",
        "hitl_need": "optional",
        "rationale": "Repeated infra failures point at orchestration recovery.",
    }
    worker = _StaticWorker(WorkerResult(status="success", summary=json.dumps(payload)))
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)

    result = await brain.score_improvement_suggestion(
        report=_report(),
        deterministic_suggestion=_suggestion(),
        context=_context(),
    )

    assert result is not None
    assert result.suggestion.effort == "medium"
    assert result.suggestion.risk == "medium"
    assert result.suggestion.layer_impact == "orchestrator"
    assert result.suggestion.validation_path == payload["validation_path"]
    assert result.metadata.mode == "llm"
    assert result.metadata.provider == "_StaticWorker"
    assert result.metadata.rationale == payload["rationale"]
    assert result.metadata.fallback is False

    request = worker.requests[0]
    assert request.session_id == "session-1"
    assert request.response_format == "json"
    assert request.response_schema is not None
    assert "value" in request.response_schema["properties"]
    assert request.constraints["read_only"] is True
    assert request.constraints["budget"] == "bounded"


@pytest.mark.asyncio
async def test_score_improvement_suggestion_normalizes_literal_case() -> None:
    payload = {
        "value": "High",
        "effort": "Medium",
        "risk": "Low",
        "layer_impact": "Orchestrator",
        "validation_path": "Run orchestrator persistence tests.",
        "hitl_need": "Optional",
        "rationale": "Capitalized model output should still validate.",
    }
    worker = _StaticWorker(WorkerResult(status="success", summary=json.dumps(payload)))
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)

    result = await brain.score_improvement_suggestion(
        report=_report(),
        deterministic_suggestion=_suggestion(),
        context=_context(),
    )

    assert result is not None
    assert result.suggestion.value == "high"
    assert result.suggestion.effort == "medium"
    assert result.suggestion.risk == "low"
    assert result.suggestion.layer_impact == "orchestrator"
    assert result.suggestion.hitl_need == "optional"


@pytest.mark.asyncio
async def test_score_improvement_suggestion_returns_none_for_invalid_payload() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(WorkerResult(status="success", summary="not json"))
    )

    result = await brain.score_improvement_suggestion(
        report=_report(),
        deterministic_suggestion=_suggestion(),
        context=_context(),
    )

    assert result is None


@pytest.mark.asyncio
async def test_score_improvement_suggestion_returns_none_on_planner_failure() -> None:
    brain = RuleBasedOrchestratorBrain(planner_worker=_ExplodingWorker())

    result = await brain.score_improvement_suggestion(
        report=_report(),
        deterministic_suggestion=_suggestion(),
        context=_context(),
    )

    assert result is None


@pytest.mark.asyncio
async def test_score_improvement_suggestion_returns_none_on_missing_planner_result() -> None:
    brain = RuleBasedOrchestratorBrain(planner_worker=_NoneWorker())

    result = await brain.score_improvement_suggestion(
        report=_report(),
        deterministic_suggestion=_suggestion(),
        context=_context(),
    )

    assert result is None
