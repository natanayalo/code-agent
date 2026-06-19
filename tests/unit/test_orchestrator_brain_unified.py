"""Unified task-spec-and-route suggestion tests for the orchestrator brain."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator.brain import RuleBasedOrchestratorBrain
from orchestrator.state import TaskSpec
from tests.unit.orchestrator_brain_support import _state, _StaticWorker
from workers import WorkerResult


@pytest.mark.anyio
async def test_suggest_task_spec_and_route_parses_unified_payload() -> None:
    worker = _StaticWorker(
        WorkerResult(
            status="success",
            summary=(
                '{"assumptions":["a1"],"acceptance_criteria":[],"non_goals":[],'
                '"clarification_questions":[],"verification_commands":[],'
                '"suggested_risk_level":null,"suggested_task_type":null,'
                '"suggested_delivery_mode":null,"suggested_worker":"antigravity",'
                '"suggested_profile":"antigravity-native-executor-read-only",'
                '"suggested_retry_strategy":null,"rationale":"use antigravity read-only"}'
            ),
        )
    )
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)
    state = _state()
    state.task_spec = TaskSpec(goal="Route this task")

    suggestion = await brain.suggest_task_spec_and_route(
        state=state,
        task_spec=state.task_spec,
        available_workers=frozenset({"codex", "antigravity"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "antigravity"
    assert suggestion.suggested_profile == "antigravity-native-executor-read-only"


@pytest.mark.anyio
async def test_suggest_task_spec_and_route_ignores_retired_worker_names() -> None:
    worker = _StaticWorker(
        WorkerResult(
            status="success",
            summary=(
                '{"session_id":"s1","response":"```json\\n'
                '{\\"assumptions\\":[],\\"acceptance_criteria\\":[],\\"non_goals\\":[],'
                '\\"clarification_questions\\":[],\\"verification_commands\\":[],'
                '\\"suggested_risk_level\\":null,\\"suggested_task_type\\":null,'
                '\\"suggested_delivery_mode\\":null,\\"suggested_worker\\":\\"gemini\\",'
                '\\"suggested_profile\\":\\"antigravity-native-executor-read-only\\",'
                '\\"suggested_retry_strategy\\":null,\\"rationale\\":\\"retired worker name\\"}'
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
        available_workers=frozenset({"codex", "antigravity"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker is None
    assert suggestion.suggested_profile == "antigravity-native-executor-read-only"


@pytest.mark.anyio
async def test_suggest_task_spec_and_route_salvages_route_when_task_type_invalid() -> None:
    worker = _StaticWorker(
        WorkerResult(
            status="success",
            summary=(
                '{"assumptions":[],"acceptance_criteria":[],"non_goals":[],'
                '"clarification_questions":[],"verification_commands":[],'
                '"suggested_risk_level":"low","suggested_task_type":"not_a_real_type",'
                '"suggested_delivery_mode":"summary","suggested_worker":"antigravity",'
                '"suggested_profile":"antigravity-native-executor-read-only",'
                '"suggested_retry_strategy":null,"rationale":"use antigravity"}'
            ),
        )
    )
    brain = RuleBasedOrchestratorBrain(planner_worker=worker)
    state = _state()
    state.task_spec = TaskSpec(goal="Route this task")

    suggestion = await brain.suggest_task_spec_and_route(
        state=state,
        task_spec=state.task_spec,
        available_workers=frozenset({"codex", "antigravity"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "antigravity"
    assert suggestion.suggested_profile == "antigravity-native-executor-read-only"
    assert suggestion.suggested_task_type is None


@pytest.mark.anyio
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
                    '"suggested_delivery_mode":"summary","suggested_worker":"antigravity",'
                    '"suggested_profile":"antigravity-native-executor-read-only",'
                    '"suggested_retry_strategy":null,"rationale":"use antigravity"}\n'
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
        available_workers=frozenset({"codex", "antigravity"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "antigravity"
    assert suggestion.suggested_profile == "antigravity-native-executor-read-only"


@pytest.mark.anyio
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
                '"suggested_delivery_mode":"summary","suggested_worker":"antigravity",'
                '"suggested_profile":"antigravity-native-executor-read-only",'
                '"suggested_retry_strategy":null,"rationale":"use antigravity"}\n'
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
        available_workers=frozenset({"codex", "antigravity"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "antigravity"
    assert suggestion.suggested_profile == "antigravity-native-executor-read-only"


@pytest.mark.anyio
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
                '"suggested_delivery_mode":"summary","suggested_worker":"antigravity",'
                '"suggested_profile":"antigravity-native-executor-read-only",'
                '"suggested_retry_strategy":null,"rationale":"use antigravity"}\n'
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
        available_workers=frozenset({"codex", "antigravity"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "antigravity"
    assert suggestion.suggested_profile == "antigravity-native-executor-read-only"


@pytest.mark.anyio
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


@pytest.mark.anyio
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

    assert worker.requests[-1].budget["worker_timeout_seconds"] == 45


@pytest.mark.anyio
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
