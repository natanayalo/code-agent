"""Route recommendation tests for the orchestrator brain."""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import patch

import pytest

from orchestrator.brain import RuleBasedOrchestratorBrain
from tests.unit.orchestrator_brain_support import (
    _ExplodingWorker,
    _SlowWorker,
    _state,
    _StaticWorker,
)
from workers import WorkerResult
from workers.constants import DEFAULT_DISCOVERY_TIMEOUT_SECONDS


def test_default_brain_timeout_is_forty_five_seconds() -> None:
    brain = RuleBasedOrchestratorBrain()

    assert DEFAULT_DISCOVERY_TIMEOUT_SECONDS == 45
    assert brain.planner_timeout_seconds == 45


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
            available_workers=frozenset({"codex", "antigravity"}),
            available_profiles=None,
        )
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "codex"
    assert suggestion.suggested_profile is None
    assert suggestion.rationale == "prefer codex"


def test_suggest_route_coerces_retired_worker_names() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(
                status="success",
                summary=(
                    '{"suggested_worker":"gemini","suggested_profile":"gemini-native-executor",'
                    '"rationale":"use gemini"}'
                ),
            )
        )
    )

    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex", "antigravity"}),
            available_profiles=None,
        )
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "antigravity"
    assert suggestion.suggested_profile == "antigravity-native-executor"
    assert suggestion.rationale == "use gemini"


def test_suggest_route_parses_fenced_json_payload() -> None:
    brain = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(
                status="success",
                summary=(
                    "```json\n"
                    '{"suggested_worker":"antigravity","suggested_profile":"antigravity-native-executor",'
                    '"rationale":"complex task"}\n'
                    "```"
                ),
            )
        )
    )

    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex", "antigravity"}),
            available_profiles=None,
        )
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "antigravity"
    assert suggestion.suggested_profile == "antigravity-native-executor"
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
                summary="worker profile 'antigravity-native-planner' is unavailable",
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
    state.task.constraints["custom_mapping"] = CustomMapping({"key": "value"})

    asyncio.run(
        brain.suggest_route(
            state=state,
            available_workers=frozenset({"codex"}),
            available_profiles=None,
        )
    )

    last_request = worker.requests[-1]
    match = re.search(r"Context JSON:\n(.*?)\n", last_request.task_text, re.DOTALL)
    assert match is not None
    json_text = match.group(1)
    payload = json.loads(json_text)

    assert payload["task_constraints"]["custom_mapping"] == {"key": "value"}
    assert "dispatch_worker" in payload


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

    suggestion = asyncio.run(
        brain.suggest_route(
            state=_state(),
            available_workers=frozenset({"codex"}),
            available_profiles=None,
        )
    )
    assert suggestion is None
