"""Unit tests for orchestrator brain route recommendation behavior."""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import patch

import pytest

from orchestrator.brain import RuleBasedOrchestratorBrain
from orchestrator.state import OrchestratorState, TaskRequest, TaskSpec
from workers import Worker, WorkerRequest, WorkerResult


class _StaticWorker(Worker):
    """Worker test double returning a predefined result."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.requests: list[WorkerRequest] = []

    async def run(
        self,
        request: WorkerRequest,
        *,
        system_prompt: str | None = None,
    ) -> WorkerResult:
        self.requests.append(request)
        assert system_prompt is not None
        return self.result


class _ExplodingWorker(Worker):
    """Worker test double raising an exception."""

    async def run(
        self,
        request: WorkerRequest,
        *,
        system_prompt: str | None = None,
    ) -> WorkerResult:
        del request, system_prompt
        raise RuntimeError("planner crashed")


class _SlowWorker(Worker):
    """Worker test double sleeping long enough to trigger timeout."""

    async def run(
        self,
        request: WorkerRequest,
        *,
        system_prompt: str | None = None,
    ) -> WorkerResult:
        del request, system_prompt
        await asyncio.sleep(1.1)
        return WorkerResult(status="success", summary='{"suggested_worker":"codex"}')


def _state() -> OrchestratorState:
    return OrchestratorState.model_validate(
        {
            "task": {"task_text": "Route this task"},
            "task_kind": "implementation",
        }
    )


def test_suggest_route_parses_plain_json_payload() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(
                status="success",
                summary=(
                    '{"suggested_worker":"codex","suggested_profile":null,'
                    '"rationale":"prefer codex"}'
                ),
            )
        )
    )

    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex", "gemini"}),
            available_profiles=None,
        )
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "codex"
    assert suggestion.suggested_profile is None
    assert suggestion.rationale == "prefer codex"


def test_suggest_route_parses_fenced_json_payload() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(
                status="success",
                summary=(
                    "```json\n"
                    '{"suggested_worker":"gemini","suggested_profile":"gemini-native-executor",'
                    '"rationale":"complex task"}\n'
                    "```"
                ),
            )
        )
    )

    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex", "gemini"}),
            available_profiles=None,
        )
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "gemini"
    assert suggestion.suggested_profile == "gemini-native-executor"
    assert suggestion.rationale == "complex task"


def test_suggest_route_requires_planner_worker() -> None:
    brain = RuleBasedOrchestratorBrain(planner_worker=None)

    with pytest.raises(RuntimeError, match="planner worker not wired"):
        asyncio.run(
            brain.suggest_route(
                state=_state(),
                available_workers=frozenset({"codex"}),
                available_profiles=None,
            )
        )


def test_suggest_route_rejects_empty_summary() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(WorkerResult(status="success", summary=""))
    )

    with pytest.raises(RuntimeError, match="empty summary"):
        asyncio.run(
            brain.suggest_route(
                state=_state(),
                available_workers=frozenset({"codex"}),
                available_profiles=None,
            )
        )


def test_suggest_route_rejects_malformed_json() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(WorkerResult(status="success", summary="not-json"))
    )

    with pytest.raises(RuntimeError, match="invalid JSON"):
        asyncio.run(
            brain.suggest_route(
                state=_state(),
                available_workers=frozenset({"codex"}),
                available_profiles=None,
            )
        )


def test_suggest_route_rejects_non_success_worker_result() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(status="failure", summary="planner could not complete")
        )
    )

    with pytest.raises(RuntimeError, match="non-success status"):
        asyncio.run(
            brain.suggest_route(
                state=_state(),
                available_workers=frozenset({"codex"}),
                available_profiles=None,
            )
        )


def test_suggest_route_surfaces_unavailable_planner_profile() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(
                status="failure",
                summary="worker profile 'gemini-native-planner' is unavailable",
            )
        )
    )

    with pytest.raises(RuntimeError, match="non-success status"):
        asyncio.run(
            brain.suggest_route(
                state=_state(),
                available_workers=frozenset({"codex"}),
                available_profiles=None,
            )
        )


def test_suggest_route_surfaces_worker_exception() -> None:
    brain = RuleBasedOrchestratorBrain(planner_worker=_ExplodingWorker())

    with pytest.raises(RuntimeError, match="planner crashed"):
        asyncio.run(
            brain.suggest_route(
                state=_state(),
                available_workers=frozenset({"codex"}),
                available_profiles=None,
            )
        )


def test_suggest_route_times_out_when_planner_slow() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_SlowWorker(),
        planner_timeout_seconds=1,
    )

    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(
            brain.suggest_route(
                state=_state(),
                available_workers=frozenset({"codex"}),
                available_profiles=None,
            )
        )


def test_suggest_route_serializes_complex_payload() -> None:
    from collections.abc import Mapping

    class CustomMapping(Mapping):
        def __init__(self, data):
            self._data = data

        def __getitem__(self, key):
            return self._data[key]

        def __iter__(self):
            return iter(self._data)

        def __len__(self):
            return len(self._data)

    worker = _StaticWorker(WorkerResult(status="success", summary='{"suggested_worker":"codex"}'))
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)

    state = _state()
    # Add some complex types to the state that should be serialized
    state.task.constraints["custom_mapping"] = CustomMapping({"key": "value"})

    asyncio.run(
        brain.suggest_route(
            state=state,
            available_workers=frozenset({"codex"}),
            available_profiles=None,
        )
    )

    # Verify that the last request's task_text contains a valid JSON representation
    # of the complex payload
    last_request = worker.requests[-1]
    # Find the JSON block in the prompt
    match = re.search(r"Context JSON:\n(.*?)\n", last_request.task_text, re.DOTALL)
    assert match is not None
    json_text = match.group(1)
    payload = json.loads(json_text)

    # Check that custom_mapping was converted to a dict
    assert payload["task_constraints"]["custom_mapping"] == {"key": "value"}
    # Check that worker types (Enums/Literals) are handled
    assert "dispatch_worker" in payload


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
    # Rules should have escalated to medium because of "Urgent"
    # Model should have escalated to high
    assert suggestion.suggested_risk_level == "high"
    assert "Model assumption" in suggestion.assumptions
    assert "Model criteria" in suggestion.acceptance_criteria
    assert "model-check" in suggestion.verification_commands
    assert "rules(rule_based_task_spec_enrichment_v1)" in suggestion.rationale
    assert "model(Model knows best)" in suggestion.rationale


@pytest.mark.asyncio
async def test_suggest_task_spec_model_timeout_falls_back_to_rules() -> None:
    brain = RuleBasedOrchestratorBrain(planner_worker=_SlowWorker())
    # Override default timeout for test speed
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

        # Should still have the rule-based escalation
        assert suggestion is not None
        assert suggestion.suggested_risk_level == "medium"
        assert suggestion.rationale == "rule_based_task_spec_enrichment_v1"


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
    assert suggestion.rationale == "rule_based_task_spec_enrichment_v1"


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
