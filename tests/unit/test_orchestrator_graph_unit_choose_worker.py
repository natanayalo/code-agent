# ruff: noqa: F403, F405
"""Choose-worker orchestrator graph unit tests."""

from __future__ import annotations

from tests.unit.orchestrator_graph_unit_support import *  # noqa: F403


def test_choose_worker_override():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "worker_override": "codex"}}
    )
    res = choose_worker(state)
    assert res["route"]["chosen_worker"] == "codex"
    assert res["route"]["route_reason"] == "manual_override"
    assert res["route"]["override_applied"] is True


def test_choose_worker_architecture_default():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "task_kind": "architecture"}
    )
    res = choose_worker(state)
    assert res["route"]["chosen_worker"] == "gemini"
    assert res["route"]["route_reason"] == "high_stakes_refactor"


def test_build_choose_worker_node_binds_available_workers():
    """build_choose_worker_node creates a node that uses the bound available workers."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "task_kind": "architecture"}
    )
    node = build_choose_worker_node(frozenset({"codex"}))
    res = asyncio.run(node(state))
    assert res["route"]["chosen_worker"] == "codex"
    assert res["route"]["route_reason"] == "preferred_unavailable"


def test_build_choose_worker_node_applies_brain_worker_suggestion() -> None:
    class _Brain:
        def suggest_task_spec(self, **kwargs):
            del kwargs
            return None

        async def suggest_route(self, **kwargs):
            del kwargs
            return RouteBrainSuggestion(
                suggested_worker="gemini",
                rationale="Prefer higher-quality reasoning for this task.",
            )

    state = OrchestratorState.model_validate(
        {"task": {"task_text": "implement change"}, "task_kind": "implementation"}
    )
    node = build_choose_worker_node(
        _ALL_WORKERS,
        orchestrator_brain=_Brain(),
    )
    res = asyncio.run(node(state))

    assert res["route"]["chosen_worker"] == "gemini"
    assert res["route"]["route_reason"] == "brain_recommendation"
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["provider"] == "_Brain"
    assert brain_payload["applied"] is True
    assert brain_payload["ignored_fields"] == []
    assert brain_payload["final_chosen_worker"] == "gemini"
    assert brain_payload["final_route_reason"] == "brain_recommendation"


def test_build_choose_worker_node_applies_brain_profile_suggestion_with_worker_clamp() -> None:
    class _Brain:
        def suggest_task_spec(self, **kwargs):
            del kwargs
            return None

        async def suggest_route(self, **kwargs):
            del kwargs
            return RouteBrainSuggestion(
                suggested_worker="codex",
                suggested_profile="gemini-native-executor",
                rationale="Use the gemini native profile despite worker hint mismatch.",
            )

    state = OrchestratorState.model_validate(
        {"task": {"task_text": "investigate behavior"}, "task_kind": "ambiguous"}
    )
    node = build_choose_worker_node(
        _ALL_WORKERS,
        available_profiles=_PROFILED_CODEX_GEMINI,
        orchestrator_brain=_Brain(),
    )
    res = asyncio.run(node(state))

    assert res["route"]["chosen_worker"] == "gemini"
    assert res["route"]["chosen_profile"] == "gemini-native-executor"
    assert res["route"]["runtime_mode"] == "native_agent"
    assert res["route"]["route_reason"] == "brain_recommendation"
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["applied"] is True
    assert brain_payload["ignored_fields"] == ["suggested_worker"]
    assert brain_payload["final_chosen_profile"] == "gemini-native-executor"


def test_build_choose_worker_node_falls_back_when_brain_suggestion_is_unavailable() -> None:
    class _Brain:
        def suggest_task_spec(self, **kwargs):
            del kwargs
            return None

        async def suggest_route(self, **kwargs):
            del kwargs
            return RouteBrainSuggestion(
                suggested_worker="openrouter",
                rationale="Prefer openrouter.",
            )

    state = OrchestratorState.model_validate(
        {"task": {"task_text": "refactor module"}, "task_kind": "architecture"}
    )
    node = build_choose_worker_node(
        _CODEX_ONLY,
        orchestrator_brain=_Brain(),
    )
    res = asyncio.run(node(state))

    assert res["route"]["chosen_worker"] == "codex"
    assert res["route"]["route_reason"] == "preferred_unavailable"
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["applied"] is False
    assert brain_payload["ignored_fields"] == ["suggested_worker"]
    assert brain_payload["final_chosen_worker"] == "codex"
    assert brain_payload["final_route_reason"] == "preferred_unavailable"


def test_build_choose_worker_node_skips_brain_for_manual_override() -> None:
    class _Brain:
        def suggest_task_spec(self, **kwargs):
            del kwargs
            return None

        async def suggest_route(self, **kwargs):
            del kwargs
            return RouteBrainSuggestion(suggested_worker="codex")

    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "worker_override": "gemini"}}
    )
    node = build_choose_worker_node(
        _ALL_WORKERS,
        orchestrator_brain=_Brain(),
    )
    res = asyncio.run(node(state))

    assert res["route"]["chosen_worker"] == "gemini"
    assert res["route"]["route_reason"] == "manual_override"
    assert "brain" not in res["timeline_events"][0].payload


def test_build_choose_worker_node_reports_brain_errors_and_falls_back() -> None:
    class _ExplodingBrain:
        def suggest_task_spec(self, **kwargs):
            del kwargs
            return None

        async def suggest_route(self, **kwargs):
            del kwargs
            raise RuntimeError("planner unavailable")

    state = OrchestratorState.model_validate(
        {"task": {"task_text": "add helper"}, "task_kind": "implementation"}
    )
    node = build_choose_worker_node(
        _ALL_WORKERS,
        orchestrator_brain=_ExplodingBrain(),
    )
    res = asyncio.run(node(state))

    assert res["route"]["chosen_worker"] == "codex"
    assert res["route"]["route_reason"] == "cheap_mechanical_change"
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["provider"] == "_ExplodingBrain"
    assert brain_payload["applied"] is False
    assert brain_payload["error"] == "RuntimeError: planner unavailable"


def test_build_choose_worker_node_applies_brain_retry_same_worker_hint() -> None:
    class _Brain:
        def suggest_task_spec(self, **kwargs):
            del kwargs
            return None

        async def suggest_route(self, **kwargs):
            del kwargs
            return RouteBrainSuggestion(
                suggested_retry_strategy="retry_same_worker",
                rationale="Retry provider/auth issues on the same worker.",
            )

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix auth issue"},
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "result": {
                "status": "error",
                "failure_kind": "provider_auth",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    node = build_choose_worker_node(
        _ALL_WORKERS,
        orchestrator_brain=_Brain(),
    )
    res = asyncio.run(node(state))

    assert res["route"]["chosen_worker"] == "codex"
    assert res["route"]["route_reason"] == "brain_retry_same_worker"
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["applied"] is True
    assert brain_payload["suggested_retry_strategy"] == "retry_same_worker"
    assert brain_payload["final_route_reason"] == "brain_retry_same_worker"


def test_build_choose_worker_node_ignores_invalid_brain_retry_hint() -> None:
    class _Brain:
        def suggest_task_spec(self, **kwargs):
            del kwargs
            return None

        async def suggest_route(self, **kwargs):
            del kwargs
            return RouteBrainSuggestion(
                suggested_retry_strategy="retry_same_worker",
                rationale="Retry on same worker.",
            )

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix tests"},
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "result": {
                "status": "failure",
                "failure_kind": "test",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    node = build_choose_worker_node(
        _ALL_WORKERS,
        orchestrator_brain=_Brain(),
    )
    res = asyncio.run(node(state))

    assert res["route"]["chosen_worker"] == "gemini"
    assert res["route"]["route_reason"] == "previous_worker_failed"
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["applied"] is False
    assert brain_payload["ignored_fields"] == ["suggested_retry_strategy"]


def test_build_choose_worker_node_applies_brain_retry_escalation_hint() -> None:
    class _Brain:
        def suggest_task_spec(self, **kwargs):
            del kwargs
            return None

        async def suggest_route(self, **kwargs):
            del kwargs
            return RouteBrainSuggestion(
                suggested_worker="openrouter",
                suggested_retry_strategy="escalate_to_alternate",
                rationale="Escalate to openrouter for alternate perspective.",
            )

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix tests"},
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "result": {
                "status": "failure",
                "failure_kind": "test",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    node = build_choose_worker_node(
        _ALL_WORKERS,
        orchestrator_brain=_Brain(),
    )
    res = asyncio.run(node(state))

    assert res["route"]["chosen_worker"] == "openrouter"
    assert res["route"]["route_reason"] == "brain_retry_escalation"
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["applied"] is True
    assert brain_payload["suggested_retry_strategy"] == "escalate_to_alternate"
    assert brain_payload["final_route_reason"] == "brain_retry_escalation"


def test_build_choose_worker_node_prevents_brain_from_bypassing_escalation() -> None:
    class _Brain:
        def suggest_task_spec(self, **kwargs):
            return None

        async def suggest_route(self, **kwargs):
            return RouteBrainSuggestion(
                suggested_worker="codex",
                rationale="Bypass escalation hint by just suggesting codex again.",
            )

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix tests"},
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "result": {
                "status": "failure",
                "failure_kind": "test",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    node = build_choose_worker_node(_ALL_WORKERS, orchestrator_brain=_Brain())
    res = asyncio.run(node(state))

    assert res["route"]["chosen_worker"] == "gemini"
    assert res["route"]["route_reason"] == "previous_worker_failed"
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["applied"] is False
    assert brain_payload["ignored_fields"] == ["suggested_worker"]


def test_build_choose_worker_node_handles_hallucinated_brain_retry_strategy() -> None:
    class _Brain:
        def suggest_task_spec(self, **kwargs):
            return None

        async def suggest_route(self, **kwargs):
            return RouteBrainSuggestion(
                suggested_retry_strategy="hallucinated_strategy",
                rationale="I am a creative LLM.",
            )

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix tests"},
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "result": {
                "status": "failure",
                "failure_kind": "test",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    node = build_choose_worker_node(_ALL_WORKERS, orchestrator_brain=_Brain())
    res = asyncio.run(node(state))

    brain_payload = res["timeline_events"][0].payload["brain"]
    assert res["route"]["chosen_worker"] == "gemini"
    assert res["route"]["route_reason"] == "previous_worker_failed"
    assert brain_payload["applied"] is False
    assert brain_payload["ignored_fields"] == ["suggested_retry_strategy"]


def test_apply_brain_retry_strategy_only_logs_provided_ignored_fields() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix tests"},
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "result": {
                "status": "failure",
                "failure_kind": "test",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    suggestion = RouteBrainSuggestion(suggested_retry_strategy="retry_same_worker")

    from orchestrator.graph import _apply_brain_retry_strategy, _resolve_brain_retry_context

    prior_worker, allowed_strategy = _resolve_brain_retry_context(state)
    route, ignored = _apply_brain_retry_strategy(
        suggestion=suggestion,
        available_workers=frozenset({"codex", "gemini"}),
        available_profiles=None,
        routable_profiles={},
        prior_worker=prior_worker,
        allowed_strategy=allowed_strategy,
    )
    assert route is None
    assert ignored == ["suggested_retry_strategy"]


def test_build_choose_worker_node_applies_brain_read_only_profile_on_mutable_task() -> None:
    class _Brain:
        def suggest_task_spec(self, **kwargs):
            del kwargs
            return None

        async def suggest_route(self, **kwargs):
            del kwargs
            return RouteBrainSuggestion(
                suggested_worker="gemini",
                suggested_profile="gemini-native-executor-read-only",
                rationale="Use read-only executor for this command-only task.",
            )

    state = OrchestratorState.model_validate(
        {"task": {"task_text": "print PWD and HOME"}, "task_kind": "implementation"}
    )
    profiles = {
        "gemini-native-executor-read-only": WorkerProfile(
            name="gemini-native-executor-read-only",
            worker_type="gemini",
            runtime_mode="native_agent",
            mutation_policy="read_only",
            capability_tags=["execution"],
        ),
        "codex-native-executor": WorkerProfile(
            name="codex-native-executor",
            worker_type="codex",
            runtime_mode="native_agent",
            mutation_policy="patch_allowed",
            capability_tags=["execution"],
        ),
    }

    node = build_choose_worker_node(
        _ALL_WORKERS,
        available_profiles=profiles,
        orchestrator_brain=_Brain(),
    )
    res = asyncio.run(node(state))

    assert res["route"]["chosen_worker"] == "gemini"
    assert res["route"]["chosen_profile"] == "gemini-native-executor-read-only"
    assert res["route"]["route_reason"] == "brain_recommendation"
