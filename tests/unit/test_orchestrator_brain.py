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
from workers.constants import DEFAULT_BRAIN_TIMEOUT_SECONDS


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


def test_default_brain_timeout_is_five_minutes() -> None:
    brain = RuleBasedOrchestratorBrain()

    assert DEFAULT_BRAIN_TIMEOUT_SECONDS == 300
    assert brain.planner_timeout_seconds == 300


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

    with pytest.raises(RuntimeError, match="no planners wired"):
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

    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex"}),
            available_profiles=None,
        )
    )
    assert suggestion is None


def test_suggest_route_rejects_malformed_json() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(WorkerResult(status="success", summary="not-json"))
    )

    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex"}),
            available_profiles=None,
        )
    )
    assert suggestion is None


def test_suggest_route_rejects_non_success_worker_result() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(status="failure", summary="planner could not complete")
        )
    )

    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex"}),
            available_profiles=None,
        )
    )
    assert suggestion is None


def test_suggest_route_non_success_sets_planner_span_outcome() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(
                status="failure",
                summary="auth method missing in planner path",
                failure_kind="provider_auth",
            )
        )
    )
    with patch("orchestrator.brain.set_span_status_from_outcome") as set_status:
        suggestion = asyncio.run(
            brain.suggest_route(
                state=_state(),
                available_workers=frozenset({"codex"}),
                available_profiles=None,
            )
        )

    assert suggestion is None
    set_status.assert_called_once()
    status_args, _ = set_status.call_args
    assert status_args[0] == "failure"
    assert "auth method missing in planner path" in status_args[1]


def test_suggest_route_surfaces_unavailable_planner_profile() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(
                status="failure",
                summary="worker profile 'gemini-native-planner' is unavailable",
            )
        )
    )

    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex"}),
            available_profiles=None,
        )
    )
    assert suggestion is None


def test_suggest_route_surfaces_worker_exception() -> None:
    brain = RuleBasedOrchestratorBrain(planner_worker=_ExplodingWorker())

    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex"}),
            available_profiles=None,
        )
    )
    assert suggestion is None


def test_suggest_route_times_out_when_planner_slow() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_SlowWorker(),
        planner_timeout_seconds=1,
    )

    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex"}),
            available_profiles=None,
        )
    )
    assert suggestion is None


def test_suggest_route_rejects_non_object_json_payload() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(WorkerResult(status="success", summary='["not-object"]'))
    )
    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex"}),
            available_profiles=None,
        )
    )
    assert suggestion is None


def test_suggest_task_spec_returns_none_on_validation_failure() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(status="success", summary='{"accept_warning_status": true}')
        )
    )
    task_spec = TaskSpec(goal="g")
    suggestion = asyncio.run(
        brain.suggest_task_spec(
            task=TaskRequest(task_text="hello"),
            task_kind="implementation",
            task_plan=None,
            task_spec=task_spec,
        )
    )
    assert suggestion is None


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


@pytest.mark.asyncio
async def test_suggest_task_spec_and_route_parses_unified_payload() -> None:
    worker = _StaticWorker(
        WorkerResult(
            status="success",
            summary=(
                '{"assumptions":["a1"],"acceptance_criteria":[],"non_goals":[],'
                '"clarification_questions":[],"verification_commands":[],'
                '"suggested_risk_level":null,"suggested_task_type":null,'
                '"suggested_delivery_mode":null,"suggested_worker":"gemini",'
                '"suggested_profile":"gemini-native-executor-read-only",'
                '"suggested_retry_strategy":null,"rationale":"use gemini read-only"}'
            ),
        )
    )
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)
    state = _state()
    state.task_spec = TaskSpec(goal="Route this task")

    suggestion = await brain.suggest_task_spec_and_route(
        state=state,
        task_spec=state.task_spec,
        available_workers=frozenset({"codex", "gemini"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "gemini"
    assert suggestion.suggested_profile == "gemini-native-executor-read-only"


@pytest.mark.asyncio
async def test_suggest_task_spec_and_route_parses_wrapped_unified_payload() -> None:
    worker = _StaticWorker(
        WorkerResult(
            status="success",
            summary=(
                '{"session_id":"s1","response":"```json\\n'
                '{\\"assumptions\\":[],\\"acceptance_criteria\\":[],\\"non_goals\\":[],'
                '\\"clarification_questions\\":[],\\"verification_commands\\":[],'
                '\\"suggested_risk_level\\":null,\\"suggested_task_type\\":null,'
                '\\"suggested_delivery_mode\\":null,\\"suggested_worker\\":\\"gemini\\",'
                '\\"suggested_profile\\":\\"gemini-native-executor-read-only\\",'
                '\\"suggested_retry_strategy\\":null,\\"rationale\\":\\"use gemini\\"}'
                '\\n```","stats":{"models":{}}}'
            ),
        )
    )
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)
    state = _state()
    state.task_spec = TaskSpec(goal="Route this task")

    suggestion = await brain.suggest_task_spec_and_route(
        state=state,
        task_spec=state.task_spec,
        available_workers=frozenset({"codex", "gemini"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "gemini"
    assert suggestion.suggested_profile == "gemini-native-executor-read-only"


@pytest.mark.asyncio
async def test_suggest_task_spec_and_route_salvages_route_when_task_type_invalid() -> None:
    worker = _StaticWorker(
        WorkerResult(
            status="success",
            summary=(
                '{"assumptions":[],"acceptance_criteria":[],"non_goals":[],'
                '"clarification_questions":[],"verification_commands":[],'
                '"suggested_risk_level":"low","suggested_task_type":"not_a_real_type",'
                '"suggested_delivery_mode":"summary","suggested_worker":"gemini",'
                '"suggested_profile":"gemini-native-executor-read-only",'
                '"suggested_retry_strategy":null,"rationale":"use gemini"}'
            ),
        )
    )
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)
    state = _state()
    state.task_spec = TaskSpec(goal="Route this task")

    suggestion = await brain.suggest_task_spec_and_route(
        state=state,
        task_spec=state.task_spec,
        available_workers=frozenset({"codex", "gemini"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "gemini"
    assert suggestion.suggested_profile == "gemini-native-executor-read-only"
    assert suggestion.suggested_task_type is None


@pytest.mark.asyncio
async def test_suggest_task_spec_and_route_unwraps_json_payload_wrapper() -> None:
    worker = _StaticWorker(
        WorkerResult(
            status="success",
            json_payload={
                "session_id": "s1",
                "response": (
                    "```json\n"
                    '{"assumptions":[],"acceptance_criteria":[],"non_goals":[],'
                    '"clarification_questions":[],"verification_commands":[],'
                    '"suggested_risk_level":"low","suggested_task_type":"maintenance",'
                    '"suggested_delivery_mode":"summary","suggested_worker":"gemini",'
                    '"suggested_profile":"gemini-native-executor-read-only",'
                    '"suggested_retry_strategy":null,"rationale":"use gemini"}\n'
                    "```"
                ),
                "stats": {"models": {}},
            },
        )
    )
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)
    state = _state()
    state.task_spec = TaskSpec(goal="Route this task")

    suggestion = await brain.suggest_task_spec_and_route(
        state=state,
        task_spec=state.task_spec,
        available_workers=frozenset({"codex", "gemini"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "gemini"
    assert suggestion.suggested_profile == "gemini-native-executor-read-only"


@pytest.mark.asyncio
async def test_suggest_task_spec_and_route_falls_back_from_stats_json_payload_to_summary() -> None:
    worker = _StaticWorker(
        WorkerResult(
            status="success",
            json_payload={
                "input": 10914,
                "prompt": 10914,
                "candidates": 367,
                "total": 12496,
                "cached": 0,
                "thoughts": 1215,
                "tool": 0,
            },
            summary=(
                "```json\n"
                '{"assumptions":[],"acceptance_criteria":[],"non_goals":[],'
                '"clarification_questions":[],"verification_commands":[],'
                '"suggested_risk_level":"low","suggested_task_type":"maintenance",'
                '"suggested_delivery_mode":"summary","suggested_worker":"gemini",'
                '"suggested_profile":"gemini-native-executor-read-only",'
                '"suggested_retry_strategy":null,"rationale":"use gemini"}\n'
                "```"
            ),
        )
    )
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)
    state = _state()
    state.task_spec = TaskSpec(goal="Route this task")

    suggestion = await brain.suggest_task_spec_and_route(
        state=state,
        task_spec=state.task_spec,
        available_workers=frozenset({"codex", "gemini"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "gemini"
    assert suggestion.suggested_profile == "gemini-native-executor-read-only"


@pytest.mark.asyncio
async def test_suggest_task_spec_and_route_prefers_fenced_unified_json_over_stats_blob() -> None:
    worker = _StaticWorker(
        WorkerResult(
            status="success",
            summary=(
                "some logs...\n"
                "```json\n"
                '{"assumptions":[],"acceptance_criteria":[],"non_goals":[],'
                '"clarification_questions":[],"verification_commands":[],'
                '"suggested_risk_level":"low","suggested_task_type":"maintenance",'
                '"suggested_delivery_mode":"summary","suggested_worker":"gemini",'
                '"suggested_profile":"gemini-native-executor-read-only",'
                '"suggested_retry_strategy":null,"rationale":"use gemini"}\n'
                "```\n"
                '{"input":10922,"prompt":10922,"candidates":349,'
                '"total":13016,"cached":0,"thoughts":1745,"tool":0}\n'
            ),
        )
    )
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)
    state = _state()
    state.task_spec = TaskSpec(goal="Route this task")

    suggestion = await brain.suggest_task_spec_and_route(
        state=state,
        task_spec=state.task_spec,
        available_workers=frozenset({"codex", "gemini"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "gemini"
    assert suggestion.suggested_profile == "gemini-native-executor-read-only"


@pytest.mark.asyncio
async def test_suggest_task_spec_and_route_uses_empty_secrets() -> None:
    worker = _StaticWorker(
        WorkerResult(
            status="success",
            summary=(
                '{"assumptions":[],"acceptance_criteria":[],"non_goals":[],'
                '"clarification_questions":[],"verification_commands":[],'
                '"suggested_risk_level":null,"suggested_task_type":null,'
                '"suggested_delivery_mode":null,"suggested_worker":"codex",'
                '"suggested_profile":null,"suggested_retry_strategy":null,'
                '"rationale":"r"}'
            ),
        )
    )
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)
    state = _state()
    state.task.secrets = {"key": "secret"}
    task_spec = TaskSpec(goal="Route this task")

    await brain.suggest_task_spec_and_route(
        state=state,
        task_spec=task_spec,
        available_workers=frozenset({"codex"}),
        available_profiles=None,
    )

    assert worker.requests[-1].secrets == {}


@pytest.mark.asyncio
async def test_suggest_task_spec_and_route_uses_default_brain_timeout_budget() -> None:
    worker = _StaticWorker(
        WorkerResult(
            status="success",
            summary=(
                '{"assumptions":[],"acceptance_criteria":[],"non_goals":[],'
                '"clarification_questions":[],"verification_commands":[],'
                '"suggested_risk_level":null,"suggested_task_type":null,'
                '"suggested_delivery_mode":null,"suggested_worker":"codex",'
                '"suggested_profile":null,"suggested_retry_strategy":null,'
                '"rationale":"r"}'
            ),
        )
    )
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)
    state = _state()
    task_spec = TaskSpec(goal="Route this task")

    await brain.suggest_task_spec_and_route(
        state=state,
        task_spec=task_spec,
        available_workers=frozenset({"codex"}),
        available_profiles=None,
    )

    assert worker.requests[-1].budget["worker_timeout_seconds"] == 300


@pytest.mark.asyncio
async def test_suggest_task_spec_and_route_records_recovered_planner_fallback() -> None:
    primary = _StaticWorker(
        WorkerResult(status="error", summary="Native agent command timed out after 120s.")
    )
    fallback = _StaticWorker(
        WorkerResult(
            status="success",
            summary=(
                '{"assumptions":[],"acceptance_criteria":[],"non_goals":[],'
                '"clarification_questions":[],"verification_commands":[],'
                '"suggested_risk_level":null,"suggested_task_type":null,'
                '"suggested_delivery_mode":null,"suggested_worker":"codex",'
                '"suggested_profile":"codex-native-executor-read-only",'
                '"suggested_retry_strategy":null,"rationale":"fallback worked"}'
            ),
        )
    )
    brain = RuleBasedOrchestratorBrain(planner_worker=primary, fallback_planners=[fallback])
    state = _state()
    task_spec = TaskSpec(goal="Route this task")

    with (
        patch("orchestrator.brain.set_current_span_attribute") as set_attr,
        patch("orchestrator.brain.add_current_span_event") as add_event,
        patch("orchestrator.brain.set_span_status_from_outcome") as set_status,
    ):
        suggestion = await brain.suggest_task_spec_and_route(
            state=state,
            task_spec=task_spec,
            available_workers=frozenset({"codex"}),
            available_profiles=None,
        )

    assert suggestion is not None
    assert suggestion.suggested_worker == "codex"
    attrs = {call.args[0]: call.args[1] for call in set_attr.call_args_list}
    assert attrs["code_agent.brain.planner_fallback.used"] is True
    assert attrs["code_agent.brain.planner_fallback.from"] == "_StaticWorker"
    assert attrs["code_agent.brain.planner_fallback.to"] == "_StaticWorker"
    assert attrs["code_agent.brain.planner_fallback.reason_code"] == "timeout"
    add_event.assert_any_call(
        "code_agent.brain.planner_failed",
        {"planner": "_StaticWorker", "status": "error", "reason_code": "timeout"},
    )
    set_status.assert_not_called()


def test_suggest_route_rejects_rationale_only_payload() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(
                status="success",
                summary='{"rationale":"only a rationale"}',
            )
        )
    )

    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex"}),
            available_profiles=None,
        )
    )
    assert suggestion is None
