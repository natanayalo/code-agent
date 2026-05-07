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
        await asyncio.sleep(10.0)
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


def test_suggest_verification_parses_plain_json_payload() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(
                status="success",
                summary=(
                    '{"accept_warning_status":true,'
                    '"rationale":"docs-only warning is acceptable"}'
                ),
            )
        )
    )
    state = _state()
    state.result = WorkerResult(
        status="success",
        files_changed=[],
        test_results=[{"name": "unit", "status": "passed"}],
        commands_run=[],
    )

    suggestion = asyncio.run(
        brain.suggest_verification(
            state=state,
            independent_verifier_outcome=None,
        )
    )

    assert suggestion is not None
    assert suggestion.accept_warning_status is True
    assert suggestion.rationale == "docs-only warning is acceptable"


def test_suggest_verification_requires_planner_worker() -> None:
    brain = RuleBasedOrchestratorBrain(planner_worker=None)
    state = _state()

    with pytest.raises(RuntimeError, match="planner worker not wired"):
        asyncio.run(
            brain.suggest_verification(
                state=state,
                independent_verifier_outcome=None,
            )
        )


def test_suggest_verification_returns_none_when_model_emits_no_hint() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(WorkerResult(status="success", summary="{}"))
    )
    state = _state()

    suggestion = asyncio.run(
        brain.suggest_verification(
            state=state,
            independent_verifier_outcome=None,
        )
    )

    assert suggestion is None


def test_suggest_verification_context_counts_failed_and_error_tests() -> None:
    worker = _StaticWorker(WorkerResult(status="success", summary="{}"))
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)
    state = _state()
    state.result = WorkerResult(
        status="success",
        files_changed=[],
        test_results=[
            {"name": "unit-pass", "status": "passed"},
            {"name": "unit-fail", "status": "failed"},
            {"name": "unit-error", "status": "error"},
        ],
        commands_run=[],
    )

    suggestion = asyncio.run(
        brain.suggest_verification(
            state=state,
            independent_verifier_outcome=None,
        )
    )

    assert suggestion is None
    last_request = worker.requests[-1]
    match = re.search(r"Context JSON:\n(.*?)\n", last_request.task_text, re.DOTALL)
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload["failed_tests_count"] == 2


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
    assert "[rules] rules_v1" in suggestion.rationale
    assert "[model] Model knows best" in suggestion.rationale


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
    # Rules say medium (via "Urgent"), Model says low
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

    # Should stay medium (from rules) instead of being downgraded to low
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
    # Result should be ["a", "b", "c", "d"]
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


@pytest.mark.asyncio
async def test_suggest_route_model_uses_empty_secrets() -> None:
    worker = _StaticWorker(WorkerResult(status="success", summary='{"suggested_worker":"codex"}'))
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)

    state = _state()
    state.task.secrets = {"key": "secret"}

    await brain.suggest_route(
        state=state,
        available_workers=frozenset({"codex"}),
        available_profiles=None,
    )

    assert worker.requests[-1].secrets == {}


def test_suggest_route_rejects_rationale_only_payload() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(
                status="success",
                summary='{"rationale":"only a rationale"}',
            )
        )
    )

    with pytest.raises(RuntimeError, match="omitted worker/profile and retry strategy hints"):
        asyncio.run(
            brain.suggest_route(
                state=_state(),
                available_workers=frozenset({"codex"}),
                available_profiles=None,
            )
        )


@pytest.mark.asyncio
async def test_suggest_task_spec_model_includes_interactions() -> None:
    from orchestrator.state import HumanInteractionSnapshot

    worker = _StaticWorker(WorkerResult(status="success", summary='{"assumptions":["test"]}'))
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)

    task = TaskRequest(task_text="Route this task")
    task_spec = TaskSpec(goal="goal")
    interactions = [
        HumanInteractionSnapshot(
            interaction_id="int-1",
            interaction_type="clarification",
            status="resolved",
            summary="Clarification request",
            response_data={"answer": "The repo is code-agent"},
        )
    ]

    await brain.suggest_task_spec(
        task=task,
        task_kind="implementation",
        task_plan=None,
        task_spec=task_spec,
        interactions=interactions,
    )

    last_request = worker.requests[-1]
    match = re.search(r"Context JSON:\n(.*?)\n", last_request.task_text, re.DOTALL)
    assert match is not None
    payload = json.loads(match.group(1))
    assert "interactions" in payload
    assert len(payload["interactions"]) == 1
    assert payload["interactions"][0]["interaction_id"] == "int-1"
    assert payload["interactions"][0]["response_data"] == {"answer": "The repo is code-agent"}
