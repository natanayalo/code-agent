"""Unit tests for the orchestrator graph internals."""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.brain import (
    RouteBrainSuggestion,
    TaskSpecBrainSuggestion,
    VerificationBrainSuggestion,
)
from orchestrator.checkpoints import create_in_memory_checkpointer
from orchestrator.graph import (
    _await_worker_with_timeout,
    _build_worker_request,
    _classify_task_kind,
    _coerce_approval_decision,
    _compute_route_decision,
    _default_worker_result_provider,
    _ensure_state,
    _resolve_orchestrator_timeout_seconds,
    _route_after_review_result,
    _task_requires_approval,
    await_approval,
    await_permission_escalation,
    build_choose_worker_node,
    choose_worker,
    dispatch_job,
    generate_task_spec,
    plan_task,
    summarize_result,
    verify_result,
)
from orchestrator.state import OrchestratorState
from orchestrator.task_spec import is_destructive_task
from workers import Worker, WorkerProfile, WorkerRequest, WorkerResult


def test_ensure_state_from_dict():
    raw_dict = {"task": {"task_text": "do something"}}
    state = _ensure_state(raw_dict)
    assert isinstance(state, OrchestratorState)
    assert state.task.task_text == "do something"


def test_classify_task_kind():
    assert _classify_task_kind("hello") == "implementation"
    assert _classify_task_kind("refactor code") == "architecture"
    assert _classify_task_kind("investigate logs") == "ambiguous"


def test_is_destructive_task():
    assert is_destructive_task("test", {"destructive_action": True}) is True


def test_task_requires_approval_ignores_untrusted_approved_status() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_id": "t1",
                "task_text": "Delete all files",
                "constraints": {"approval": {"status": "approved", "source": "user"}},
            }
        }
    )
    assert _task_requires_approval(state) is True


def test_task_requires_approval_accepts_trusted_approved_status() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_id": "t1",
                "task_text": "Delete all files",
                "constraints": {"approval": {"status": "approved", "source": "api"}},
            }
        }
    )
    assert _task_requires_approval(state) is False


def test_coerce_approval_decision():
    # boolean
    assert _coerce_approval_decision(True) is True
    # dict with boolean
    assert _coerce_approval_decision({"approved": True}) is True
    assert _coerce_approval_decision({"approved": False}) is False
    # dict with string
    assert _coerce_approval_decision({"approved": "y"}) is True
    assert _coerce_approval_decision({"approved": "no"}) is False
    # dict with invalid value
    assert _coerce_approval_decision({"approved": 123}) is False
    # empty or irrelevant dict
    assert _coerce_approval_decision({"other": "field"}) is False
    # direct string
    assert _coerce_approval_decision("yes") is True
    assert _coerce_approval_decision("no") is False


def test_default_worker_result_provider():
    request = WorkerRequest(task_text="demo")
    res = _default_worker_result_provider(request)
    assert res.status == "success"


def test_build_worker_request_from_state():
    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "session-1",
                "user_id": "user-1",
                "channel": "telegram",
                "external_thread_id": "thread-1",
            },
            "task": {
                "task_text": "Add worker interface",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "task/t-040-worker-interface",
                "constraints": {"requires_approval": False},
                "budget": {"max_minutes": 15},
            },
            "task_spec": {
                "goal": "Add worker interface",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "target_branch": "task/t-040-worker-interface",
                "risk_level": "medium",
                "task_type": "feature",
                "delivery_mode": "workspace",
            },
            "task_plan": {
                "triggered": True,
                "complexity_reason": "architecture",
                "steps": [
                    {
                        "step_id": "1",
                        "title": "Inspect",
                        "expected_outcome": "Find target files",
                    }
                ],
            },
            "route": {
                "chosen_worker": "codex",
                "chosen_profile": "codex-native-executor",
                "runtime_mode": "native_agent",
            },
        }
    )
    request = _build_worker_request(state)
    assert request.session_id == "session-1"
    assert request.repo_url == "https://github.com/natanayalo/code-agent"
    assert request.branch == "task/t-040-worker-interface"
    assert request.task_text == "Add worker interface"
    assert request.task_plan is not None
    assert request.task_plan["complexity_reason"] == "architecture"
    assert request.task_spec is not None
    assert request.task_spec["goal"] == "Add worker interface"
    assert request.constraints == {"requires_approval": False}
    assert request.budget == {"max_minutes": 15}
    assert request.worker_profile == "codex-native-executor"
    assert request.runtime_mode == "native_agent"


def test_build_worker_request_prefers_review_repair_handoff_text():
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Original task",
                "constraints": {"independent_review_repair_request": "Repair follow-up task"},
            },
            "normalized_task_text": "Normalized original task",
        }
    )

    request = _build_worker_request(state)

    assert request.task_text == "Repair follow-up task"


def test_build_worker_request_combines_verifier_and_review_repair_handoff_text():
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Original task",
                "constraints": {
                    "independent_verifier_repair_request": "Verifier repair follow-up task",
                    "independent_review_repair_request": "Review repair follow-up task",
                },
            },
            "normalized_task_text": "Normalized original task",
        }
    )

    request = _build_worker_request(state)

    assert request.task_text.startswith("Apply the following repair instructions in one pass.")
    assert "Verifier repair instructions:" in request.task_text
    assert "Verifier repair follow-up task" in request.task_text
    assert "Independent review repair instructions:" in request.task_text
    assert "Review repair follow-up task" in request.task_text


def test_plan_task_skips_simple_tasks():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "Add a helper"}, "task_kind": "implementation"}
    )

    res = plan_task(state)

    assert res["current_step"] == "plan_task"
    assert res["task_plan"] is None
    assert res["progress_updates"][-1] == "planning skipped: task is straightforward"


def test_plan_task_generates_plan_for_complex_tasks():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Refactor architecture across files"},
            "task_kind": "architecture",
            "normalized_task_text": "Refactor architecture across files",
        }
    )

    res = plan_task(state)

    assert res["current_step"] == "plan_task"
    assert res["task_plan"]["triggered"] is True
    assert res["task_plan"]["complexity_reason"] == "architectural_task"
    assert len(res["task_plan"]["steps"]) == 3
    assert res["progress_updates"][-1] == "structured plan generated (architectural_task)"


def test_plan_task_parameterizes_steps_for_ambiguous_tasks():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Investigate flaky behavior in worker runs"},
            "task_kind": "ambiguous",
        }
    )

    res = plan_task(state)

    assert res["task_plan"]["complexity_reason"] == "ambiguous_task"
    assert res["task_plan"]["steps"][0]["title"] == "Investigate Root Cause and Scope"


def test_plan_task_parameterizes_multi_file_execution_step():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Implement change across files in orchestrator"},
            "task_kind": "implementation",
        }
    )

    res = plan_task(state)

    assert res["task_plan"]["complexity_reason"] == "multi_file_task"
    assert res["task_plan"]["steps"][1]["title"] == "Sequence Multi-file Changes Safely"


def test_plan_task_detects_multifile_compound_marker():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Apply multifile change across orchestrator modules"},
            "task_kind": "implementation",
        }
    )

    res = plan_task(state)

    assert res["task_plan"]["complexity_reason"] == "multi_file_task"


@pytest.mark.asyncio
async def test_generate_task_spec_creates_policy_checked_contract_before_routing() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Delete all generated files",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "master",
            },
            "task_kind": "implementation",
        }
    )

    res = await generate_task_spec(state)

    assert res["current_step"] == "generate_task_spec"
    assert res["task_spec"]["goal"] == "Delete all generated files"
    assert res["task_spec"]["requires_permission"] is True
    assert res["task_spec"]["risk_level"] == "high"
    assert res["timeline_events"][0].event_type == "task_spec_generated"
    assert res["timeline_events"][0].payload["policy_violations"] == []


@pytest.mark.asyncio
async def test_generate_task_spec_applies_brain_enrichment_with_policy_clamps() -> None:
    class _FakeBrain:
        async def suggest_task_spec(self, **kwargs):
            del kwargs
            return TaskSpecBrainSuggestion(
                acceptance_criteria=["Document verifier pass/fail details in the summary."],
                verification_commands=["pytest tests/unit/test_orchestrator_graph_unit.py -q"],
                suggested_risk_level="high",
                suggested_task_type="docs",
                rationale="Increase scrutiny for risky workflow change.",
            )

    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Investigate flaky verifier behavior",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "main",
            },
            "task_kind": "implementation",
        }
    )

    res = await generate_task_spec(state, orchestrator_brain=_FakeBrain())

    assert res["task_spec"]["risk_level"] == "high"
    assert res["task_spec"]["requires_permission"] is True
    assert res["task_spec"]["task_type"] == "investigation"
    assert res["task_spec"]["verification_commands"] == [
        "pytest tests/unit/test_orchestrator_graph_unit.py -q"
    ]
    assert "task spec generated with brain enrichment" in res["progress_updates"]
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["provider"] == "_FakeBrain"
    assert brain_payload["applied"] is True
    assert brain_payload["ignored_fields"] == ["suggested_task_type"]
    assert brain_payload["added_acceptance_criteria"] == [
        "Document verifier pass/fail details in the summary."
    ]
    assert brain_payload["added_verification_commands"] == [
        "pytest tests/unit/test_orchestrator_graph_unit.py -q"
    ]


@pytest.mark.asyncio
async def test_generate_task_spec_brain_failures_fall_back_to_deterministic_spec() -> None:
    class _ExplodingBrain:
        async def suggest_task_spec(self, **kwargs):
            del kwargs
            raise RuntimeError("brain unavailable")

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Add API pagination"},
            "task_kind": "implementation",
        }
    )

    res = await generate_task_spec(state, orchestrator_brain=_ExplodingBrain())

    assert res["task_spec"]["goal"] == "Add API pagination"
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["provider"] == "_ExplodingBrain"
    assert "RuntimeError: brain unavailable" == brain_payload["error"]


def test_plan_task_complexity_marker_uses_word_boundaries():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Rename the multi-file-uploader module"},
            "task_kind": "implementation",
        }
    )

    res = plan_task(state)

    assert res["task_plan"] is None
    assert res["progress_updates"][-1] == "planning skipped: task is straightforward"


def test_resolve_orchestrator_timeout_seconds_prefers_explicit_override() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Run a worker",
                "budget": {
                    "orchestrator_timeout_seconds": "45",
                    "worker_timeout_seconds": 12,
                    "max_minutes": 9,
                },
            }
        }
    )

    assert _resolve_orchestrator_timeout_seconds(state) == 45


def test_resolve_orchestrator_timeout_seconds_accepts_float_like_strings() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Run a worker",
                "budget": {"orchestrator_timeout_seconds": "45.0"},
            }
        }
    )

    assert _resolve_orchestrator_timeout_seconds(state) == 45


def test_resolve_orchestrator_timeout_seconds_falls_back_to_worker_budget() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Run a worker",
                "budget": {"worker_timeout_seconds": 12},
            }
        }
    )

    assert _resolve_orchestrator_timeout_seconds(state) == 42


def test_resolve_orchestrator_timeout_seconds_falls_back_to_max_minutes() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Run a worker",
                "budget": {"max_minutes": 2},
            }
        }
    )

    assert _resolve_orchestrator_timeout_seconds(state) == 150


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


# ---------------------------------------------------------------------------
# _compute_route_decision unit tests (T-071 / T-072)
# ---------------------------------------------------------------------------

_ALL_WORKERS: frozenset[str] = frozenset({"codex", "gemini", "openrouter"})
_CODEX_ONLY: frozenset[str] = frozenset({"codex"})
_GEMINI_ONLY: frozenset[str] = frozenset({"gemini"})
_OPENROUTER_ONLY: frozenset[str] = frozenset({"openrouter"})

_PROFILED_CODEX_GEMINI: dict[str, WorkerProfile] = {
    "codex-native-executor": WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
        capability_tags=["execution"],
        supported_delivery_modes=["workspace", "branch", "draft_pr"],
    ),
    "gemini-native-executor": WorkerProfile(
        name="gemini-native-executor",
        worker_type="gemini",
        runtime_mode="native_agent",
        capability_tags=["execution"],
        supported_delivery_modes=["workspace", "branch", "draft_pr"],
    ),
}

_PROFILED_CODEX_OPENROUTER: dict[str, WorkerProfile] = {
    "codex-native-executor": WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
        capability_tags=["execution"],
        supported_delivery_modes=["workspace", "branch", "draft_pr"],
    ),
    "openrouter-tool-loop-legacy": WorkerProfile(
        name="openrouter-tool-loop-legacy",
        worker_type="openrouter",
        runtime_mode="tool_loop",
        capability_tags=["execution"],
        supported_delivery_modes=["workspace"],
    ),
}


def test_compute_route_override_available():
    """T-072: manual override is honoured when the worker is available."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "worker_override": "gemini"}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "manual_override"
    assert route.override_applied is True


def test_compute_route_override_unavailable_fails_explicitly():
    """T-072: manual override for an unavailable worker returns runtime_unavailable."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "worker_override": "gemini"}}
    )
    route = _compute_route_decision(state, _CODEX_ONLY)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "runtime_unavailable"
    assert route.override_applied is True


def test_compute_route_codex_override_available():
    """T-072: codex manual override is honoured."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "worker_override": "codex"}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "manual_override"


def test_compute_route_profile_aware_selects_native_default_for_codex() -> None:
    """Profile-aware routing should attach codex native profile metadata by default."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "add helper"}, "task_kind": "implementation"}
    )
    route = _compute_route_decision(
        state,
        _ALL_WORKERS,
        available_profiles=_PROFILED_CODEX_GEMINI,
    )

    assert route.chosen_worker == "codex"
    assert route.chosen_profile == "codex-native-executor"
    assert route.runtime_mode == "native_agent"
    assert route.route_reason == "cheap_mechanical_change"


def test_compute_route_profile_aware_keeps_legacy_tool_loop_out_of_default_selection() -> None:
    """Native codex profile should stay the default even when legacy tool-loop is configured."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "add helper"}, "task_kind": "implementation"}
    )
    profiles = {
        "codex-native-executor": WorkerProfile(
            name="codex-native-executor",
            worker_type="codex",
            runtime_mode="native_agent",
            capability_tags=["execution"],
            supported_delivery_modes=["workspace", "branch", "draft_pr"],
        ),
        "codex-tool-loop-executor": WorkerProfile(
            name="codex-tool-loop-executor",
            worker_type="codex",
            runtime_mode="tool_loop",
            capability_tags=["execution"],
            supported_delivery_modes=["workspace", "branch", "draft_pr"],
            metadata={"legacy_mode": True},
        ),
    }
    route = _compute_route_decision(
        state,
        frozenset({"codex"}),
        available_profiles=profiles,
    )

    assert route.chosen_worker == "codex"
    assert route.chosen_profile == "codex-native-executor"
    assert route.runtime_mode == "native_agent"
    assert route.route_reason == "cheap_mechanical_change"


def test_compute_route_profile_override_allows_explicit_legacy_tool_loop_opt_in() -> None:
    """Explicit profile overrides should still allow codex legacy tool-loop execution."""
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "use explicit legacy profile",
                "worker_profile_override": "codex-tool-loop-executor",
            }
        }
    )
    profiles = {
        "codex-native-executor": WorkerProfile(
            name="codex-native-executor",
            worker_type="codex",
            runtime_mode="native_agent",
            capability_tags=["execution"],
            supported_delivery_modes=["workspace", "branch", "draft_pr"],
        ),
        "codex-tool-loop-executor": WorkerProfile(
            name="codex-tool-loop-executor",
            worker_type="codex",
            runtime_mode="tool_loop",
            capability_tags=["execution"],
            supported_delivery_modes=["workspace", "branch", "draft_pr"],
            metadata={"legacy_mode": True},
        ),
    }
    route = _compute_route_decision(
        state,
        frozenset({"codex"}),
        available_profiles=profiles,
    )

    assert route.chosen_worker == "codex"
    assert route.chosen_profile == "codex-tool-loop-executor"
    assert route.runtime_mode == "tool_loop"
    assert route.route_reason == "manual_profile_override"
    assert route.override_applied is True


def test_compute_route_profile_override_unavailable_fails_explicitly() -> None:
    """Unavailable profile overrides should fail explicitly rather than silently falling back."""
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "run with openrouter profile",
                "worker_profile_override": "openrouter-tool-loop-legacy",
            }
        }
    )
    route = _compute_route_decision(
        state,
        frozenset({"codex", "gemini"}),
        available_profiles=_PROFILED_CODEX_GEMINI,
    )

    assert route.chosen_worker is None
    assert route.chosen_profile == "openrouter-tool-loop-legacy"
    assert route.runtime_mode is None
    assert route.route_reason == "runtime_unavailable"
    assert route.override_applied is True


def test_compute_route_profile_aware_openrouter_requires_legacy_opt_in() -> None:
    """OpenRouter should only participate in fallback routing when its profile is configured."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "budget": {"prefer_high_quality": True}}}
    )

    without_openrouter = _compute_route_decision(
        state,
        frozenset({"codex", "openrouter"}),
        available_profiles={
            "codex-native-executor": _PROFILED_CODEX_OPENROUTER["codex-native-executor"]
        },
    )
    with_openrouter = _compute_route_decision(
        state,
        frozenset({"codex", "openrouter"}),
        available_profiles=_PROFILED_CODEX_OPENROUTER,
    )

    assert without_openrouter.chosen_worker == "codex"
    assert without_openrouter.chosen_profile == "codex-native-executor"
    assert without_openrouter.route_reason == "preferred_unavailable"

    assert with_openrouter.chosen_worker == "openrouter"
    assert with_openrouter.chosen_profile == "openrouter-tool-loop-legacy"
    assert with_openrouter.runtime_mode == "tool_loop"
    assert with_openrouter.route_reason == "preferred_unavailable"


def test_compute_route_budget_prefer_high_quality():
    """T-071: prefer_high_quality budget hint routes to gemini."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "budget": {"prefer_high_quality": True}}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "budget_preference"


def test_compute_route_budget_prefer_low_cost():
    """T-071: prefer_low_cost budget hint routes to codex."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "budget": {"prefer_low_cost": True}}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "budget_preference"


def test_compute_route_budget_prefer_high_quality_fallback_when_gemini_unavailable():
    """T-071: falls back to openrouter with preferred_unavailable when gemini isn't configured."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "budget": {"prefer_high_quality": True}}}
    )
    route = _compute_route_decision(state, _OPENROUTER_ONLY)
    assert route.chosen_worker == "openrouter"
    assert route.route_reason == "preferred_unavailable"


def test_compute_route_task_kind_architecture():
    """T-071: architecture task shape routes to gemini with high_stakes_refactor."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "refactor the module"}, "task_kind": "architecture"}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "high_stakes_refactor"


def test_compute_route_task_kind_ambiguous():
    """T-071: ambiguous task shape routes to gemini with ambiguous_task."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "investigate logs"}, "task_kind": "ambiguous"}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "ambiguous_task"


def test_compute_route_task_kind_implementation():
    """T-071: implementation task shape routes to codex with cheap_mechanical_change."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "add a helper"}, "task_kind": "implementation"}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "cheap_mechanical_change"


def test_compute_route_task_kind_implementation_fallback_when_codex_unavailable():
    """T-071: falls back to openrouter with preferred_unavailable when codex is unavailable."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "add a helper"}, "task_kind": "implementation"}
    )
    route = _compute_route_decision(state, _OPENROUTER_ONLY)
    assert route.chosen_worker == "openrouter"
    assert route.route_reason == "preferred_unavailable"


def test_compute_route_task_text_explicit_highest_quality_preference():
    """Explicit caller wording should request the high-quality route even without budget flags."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "Use the highest quality worker for this change"}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "budget_preference"


def test_compute_route_task_text_explicit_low_cost_preference():
    """Explicit caller wording should request the lower-cost route even without budget flags."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "Keep this as low cost as possible"}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "budget_preference"


def test_compute_route_task_text_low_cost_marker_uses_word_boundaries():
    """Low-cost marker should not match unrelated substrings like 'slow cost'."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "Choose based on slow cost convergence, not budget preference"}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "cheap_mechanical_change"


def test_compute_route_task_text_high_quality_marker_uses_word_boundaries():
    """High-quality marker should not match unrelated substrings like 'highlight quality'."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "Please highlight quality concerns in the summary only"}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "cheap_mechanical_change"


def test_compute_route_multi_file_task_prefers_high_quality_worker():
    """Implementation tasks that span many files should use the higher-quality route."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Implement this change across files in orchestrator and workers"},
            "task_kind": "implementation",
        }
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "high_stakes_refactor"


def test_compute_route_verifier_failure_escalation():
    """T-071: failed prior verifier escalates to alternate worker."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix the code"},
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "verification": {
                "status": "failed",
                "summary": "tests failed",
                "items": [{"label": "worker_status", "status": "failed"}],
            },
            "result": {
                "status": "failure",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "verifier_failed_previous_run"


def test_compute_route_previous_worker_failed_escalation():
    """T-071: non-success result with no prior verifier escalates via previous_worker_failed."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix the code"},
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "result": {
                "status": "failure",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "previous_worker_failed"


def test_compute_route_uses_worker_failure_kind_when_verifier_failure_kind_does_not_reroute():
    """Rerouteable worker failures must still escalate after non-rerouteable verifier failures."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix the code"},
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "verification": {
                "status": "failed",
                "failure_kind": "worker_failure",
                "summary": "worker status failed",
                "items": [{"label": "worker_status", "status": "failed"}],
            },
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
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "previous_worker_failed"


def test_compute_route_retries_same_worker_for_environment_failure_kind():
    """Environment/auth failures should retry the same worker instead of cross-worker reroute."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix the code"},
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
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "environment_retry_same_worker"


def test_compute_route_environment_failure_does_not_downgrade_prior_high_quality_worker():
    """An environment failure on gemini should not silently downgrade to codex on retry."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix the code"},
            "task_kind": "implementation",
            "attempt_count": 1,
            "dispatch": {"worker_type": "gemini"},
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
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "environment_retry_same_worker"


def test_compute_route_escalation_fails_explicitly_when_alternate_unavailable():
    """T-071: escalation needed but alternate unavailable → explicit failure, not blind retry."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix the code"},
            "task_kind": "implementation",
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "verification": {
                "status": "failed",
                "summary": "tests failed",
                "items": [{"label": "worker_status", "status": "failed"}],
            },
            "result": {
                "status": "failure",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    # Only codex is available; gemini escalation not possible → explicit failure, not blind retry.
    route = _compute_route_decision(state, _CODEX_ONLY)
    assert route.chosen_worker == "gemini"  # desired alternate
    assert route.route_reason == "runtime_unavailable"


def test_compute_route_openrouter_override_available() -> None:
    """T-072: openrouter manual override is honoured when available."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "worker_override": "openrouter"}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "openrouter"
    assert route.route_reason == "manual_override"
    assert route.override_applied is True


def test_build_choose_worker_node_binds_available_workers():
    """build_choose_worker_node creates a node that uses the bound available workers."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "task_kind": "architecture"}
    )
    # With only codex available, gemini-preferred architecture task falls back to codex.
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

    assert res["route"]["chosen_worker"] == "gemini"  # Deterministic escalation
    assert res["route"]["route_reason"] == "previous_worker_failed"
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["applied"] is False
    assert brain_payload["ignored_fields"] == ["suggested_worker"]


def test_apply_brain_retry_strategy_only_logs_provided_ignored_fields() -> None:
    # Manual unit test of the internal function
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
    # Brain suggests retry_same_worker (invalid for test failure) but NO suggested_worker
    suggestion = RouteBrainSuggestion(suggested_retry_strategy="retry_same_worker")

    # We need to call the internal _apply_brain_retry_strategy
    from orchestrator.graph import _apply_brain_retry_strategy

    route, ignored = _apply_brain_retry_strategy(
        state=state,
        suggestion=suggestion,
        available_workers=frozenset({"codex", "gemini"}),
        available_profiles=None,
        routable_profiles={},
    )
    assert route is None
    assert ignored == ["suggested_retry_strategy"]  # suggested_worker should NOT be here


def test_compute_route_neither_worker_available():
    """_route_by_preference keeps the preferred intent when neither worker is available."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "task_kind": "architecture"}
    )
    # Empty available set: neither gemini nor codex is configured.
    route = _compute_route_decision(state, frozenset())
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "runtime_unavailable"


def test_compute_route_escalation_skipped_when_prior_attempt_succeeded():
    """T-071: no escalation when the prior attempt produced a successful result."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix the code"},
            "task_kind": "implementation",
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "result": {
                "status": "success",
                "commands_run": [],
                "files_changed": ["x.py"],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    # Prior attempt succeeded → escalation skipped → falls through to task shape.
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "cheap_mechanical_change"

    state = OrchestratorState.model_validate({"task": {"task_text": "demo"}})
    state.approval.required = False
    res = await_approval(state)
    assert res["current_step"] == "await_approval"


def test_summarize_result_no_result():
    state = OrchestratorState.model_validate({"task": {"task_text": "demo"}})
    res = summarize_result(state)
    assert res["result"]["status"] == "error"


def test_summarize_result_uses_normalized_task_text_for_active_goal():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "  demo  "},
            "normalized_task_text": "demo",
            "result": {
                "status": "success",
                "summary": "done",
                "commands_run": [],
                "files_changed": ["demo.txt"],
                "test_results": [],
                "artifacts": [],
            },
        }
    )

    res = summarize_result(state)

    assert res["session_state_update"]["active_goal"] == "demo"
    assert res["session_state_update"]["files_touched"] == ["demo.txt"]


def test_summarize_result_attaches_task_plan_artifact_when_present():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "dispatch": {"worker_type": "codex"},
            "task_plan": {
                "triggered": True,
                "complexity_reason": "ambiguous_task",
                "steps": [
                    {
                        "step_id": "1",
                        "title": "Inspect",
                        "expected_outcome": "Find root cause",
                    }
                ],
            },
            "result": {
                "status": "success",
                "summary": "done",
                "commands_run": [],
                "files_changed": ["demo.txt"],
                "test_results": [],
                "artifacts": [],
            },
        }
    )

    res = summarize_result(state)

    artifact = res["result"]["artifacts"][0]
    assert artifact["name"] == "task_plan"
    assert artifact["artifact_type"] == "result_summary"
    assert artifact["uri"].startswith("data:application/json;base64,")


def test_summarize_result_reviewer_findings_without_leading_newline_when_summary_empty():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "summary": "",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
            "review": {
                "reviewer_kind": "independent_reviewer",
                "summary": "Advisory findings detected.",
                "confidence": 0.8,
                "outcome": "findings",
                "findings": [
                    {
                        "severity": "medium",
                        "category": "correctness",
                        "confidence": 0.7,
                        "file_path": "orchestrator/review.py",
                        "line_start": 1,
                        "line_end": 1,
                        "title": "Example finding",
                        "why_it_matters": "Example impact.",
                    }
                ],
            },
        }
    )

    res = summarize_result(state)
    summary = res["result"]["summary"]

    assert summary.startswith("---\n### Reviewer Findings")
    assert not summary.startswith("\n")


def test_create_in_memory_checkpointer():
    cp = create_in_memory_checkpointer()
    assert cp is not None


def test_dispatch_job_preserves_attempt_count():
    """dispatch_job must preserve attempt_count (it is managed externally)."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {"chosen_worker": "codex", "route_reason": "cheap_mechanical_change"},
            "attempt_count": 0,
        }
    )
    result = dispatch_job(state)
    assert result["current_step"] == "dispatch_job"
    assert result["repair_handoff_requested"] is False


def test_dispatch_job_preserves_attempt_count_on_retry():
    """attempt_count remains constant throughout a single graph invocation."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {"chosen_worker": "gemini", "route_reason": "verifier_failed_previous_run"},
            "attempt_count": 1,
        }
    )
    result = dispatch_job(state)
    assert result["current_step"] == "dispatch_job"
    assert result["repair_handoff_requested"] is False


def test_dispatch_job_includes_route_profile_metadata() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {
                "chosen_worker": "codex",
                "chosen_profile": "codex-native-executor",
                "runtime_mode": "native_agent",
            },
        }
    )

    result = dispatch_job(state)

    assert result["dispatch"]["worker_type"] == "codex"
    assert result["dispatch"]["worker_profile"] == "codex-native-executor"
    assert result["dispatch"]["runtime_mode"] == "native_agent"


def test_route_after_review_result_dispatches_on_repair_handoff():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "repair_handoff_requested": True}
    )

    assert _route_after_review_result(state) == "dispatch_job"


def test_route_after_review_result_summarizes_without_repair_handoff():
    state = OrchestratorState.model_validate({"task": {"task_text": "demo"}})

    assert _route_after_review_result(state) == "summarize_result"


def test_await_permission_escalation_approved():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "failure",
                "next_action_hint": "request_higher_permission",
                "requested_permission": "networked_write",
                "summary": "needs high permission",
            },
        }
    )
    with patch("orchestrator.graph.interrupt", return_value=True):
        res = await_permission_escalation(state)
    assert res["current_step"] == "await_permission_escalation"
    assert res["result"] is None
    assert res["task"]["constraints"]["granted_permission"] == "networked_write"


def test_await_permission_escalation_rejected():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "failure",
                "next_action_hint": "request_higher_permission",
                "requested_permission": "networked_write",
                "summary": "needs high permission",
            },
        }
    )
    with patch("orchestrator.graph.interrupt", return_value=False):
        res = await_permission_escalation(state)
    assert res["current_step"] == "await_permission_escalation"
    assert (
        res["result"]["summary"]
        == "Permission escalation to 'networked_write' was rejected. Run halted."
    )
    assert res["result"]["failure_kind"] == "permission_denied"
    assert res["result"]["next_action_hint"] == "await_manual_follow_up"


def test_await_permission_escalation_missing_permission():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "failure",
                "next_action_hint": "request_higher_permission",
                "summary": "needs high permission",
            },
        }
    )
    res = await_permission_escalation(state)
    assert res["current_step"] == "await_permission_escalation"
    assert res["result"]["status"] == "error"
    assert res["result"]["next_action_hint"] == "inspect_worker_configuration"


def test_await_permission_escalation_invalid_permission():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "failure",
                "next_action_hint": "request_higher_permission",
                "requested_permission": "network_write",
                "summary": "needs high permission",
            },
        }
    )
    res = await_permission_escalation(state)
    assert res["current_step"] == "await_permission_escalation"
    assert res["result"]["status"] == "error"
    assert (
        res["result"]["summary"] == "Worker requested an unknown permission level 'network_write'."
    )
    assert res["result"]["requested_permission"] is None
    assert res["result"]["next_action_hint"] == "inspect_worker_configuration"


@pytest.mark.anyio
async def test_await_worker_with_timeout_partial_result():
    class SlowWorker(Worker):
        async def run(self, request):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return WorkerResult(
                    status="error",
                    summary="partial state flushed",
                    next_action_hint="inspect_workspace_artifacts",
                    commands_run=[{"command": "echo 1"}],
                )
            return WorkerResult(status="success", summary="done")

    worker = SlowWorker()
    res, hint = await _await_worker_with_timeout(
        worker,
        request=WorkerRequest(session_id="test", task_text="test"),
        worker_type="slow",
        session_id="test",
        timeout_seconds=1,
    )

    assert res.status == "error"
    assert res.summary == "partial state flushed"
    assert res.commands_run[0].command == "echo 1"
    assert hint == "worker timed out but yielded partial state after 1s"


def test_verify_result_passed():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": ["file1.py"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [{"command": "pytest", "exit_code": 0}],
            },
        }
    )
    res = verify_result(state)
    assert res["current_step"] == "verify_result"
    assert res["verification"]["status"] == "passed"
    # Status, Tests, Files, Commands
    assert len(res["verification"]["items"]) == 4


def test_verify_result_failed_tests():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": ["file1.py"],
                "test_results": [{"name": "test1", "status": "failed"}],
            },
        }
    )
    res = verify_result(state)
    assert res["verification"]["status"] == "failed"
    assert res["verification"]["failure_kind"] == "test_regression"
    assert res["verification"]["items"][1]["label"] == "test_results"
    assert res["verification"]["items"][1]["status"] == "failed"


def test_verify_result_warning_no_changes():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": [],
                "test_results": [{"name": "test1", "status": "passed"}],
            },
        }
    )
    res = verify_result(state)
    assert res["verification"]["status"] == "warning"
    assert res["verification"]["items"][2]["label"] == "file_changes"
    assert res["verification"]["items"][2]["status"] == "warning"


def test_verify_result_accepts_warning_status_from_brain_hint() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": [],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
            },
        }
    )

    res = verify_result(
        state,
        verification_brain_suggestion=VerificationBrainSuggestion(
            accept_warning_status=True,
            rationale="Warnings are acceptable for this documentation-only task.",
        ),
    )

    assert res["verification"]["status"] == "passed"
    assert res["progress_updates"][-1] == "verification passed via brain warning-acceptance hint"
    brain_payload = res["timeline_events"][1].payload["brain"]
    assert brain_payload["applied"] is True
    assert brain_payload["accept_warning_status"] is True
    assert brain_payload["final_verification_status"] == "passed"


def test_verify_result_rejects_brain_warning_acceptance_for_failed_checks() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": ["file1.py"],
                "test_results": [{"name": "test1", "status": "failed"}],
                "commands_run": [],
            },
        }
    )

    res = verify_result(
        state,
        verification_brain_suggestion=VerificationBrainSuggestion(
            accept_warning_status=True,
            rationale="Try to continue despite failures.",
        ),
    )

    assert res["verification"]["status"] == "failed"
    brain_payload = res["timeline_events"][1].payload["brain"]
    assert brain_payload["applied"] is False
    assert brain_payload["ignored_fields"] == ["accept_warning_status"]
    assert brain_payload["final_verification_status"] == "failed"


def test_verify_result_failed_with_changes():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "failure",
                "files_changed": ["partial.py"],
                "test_results": [],
            },
        }
    )
    res = verify_result(state)
    # Failed worker status makes it failed overall, but check file_changes warning
    assert res["verification"]["status"] == "failed"
    # Find file_changes item
    file_changes = next(i for i in res["verification"]["items"] if i["label"] == "file_changes")
    assert file_changes["status"] == "warning"
    assert "but changed 1 files" in file_changes["message"]


def test_verify_result_queues_repair_for_repairable_worker_failure() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo", "budget": {"max_retries": 1}},
            "result": {
                "status": "failure",
                "failure_kind": "compile",
                "summary": "Compile step failed.",
                "files_changed": ["orchestrator/graph.py"],
                "test_results": [],
                "commands_run": [{"command": "pytest", "exit_code": 1}],
            },
        }
    )

    res = verify_result(state)

    assert res["verification"]["status"] == "failed"
    assert res["verification"]["failure_kind"] == "worker_failure"
    assert res["repair_handoff_requested"] is True
    assert "queued bounded repair handoff (1/1)" in res["progress_updates"][-1]
    assert res["task"]["constraints"]["independent_verifier_repair_passes_used"] == 1


def test_verify_result_skips_repair_for_non_repairable_worker_failure() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo", "budget": {"max_retries": 1}},
            "result": {
                "status": "failure",
                "failure_kind": "provider_error",
                "summary": "Provider unavailable.",
                "files_changed": [],
                "test_results": [],
                "commands_run": [],
            },
        }
    )

    res = verify_result(state)

    assert res["verification"]["status"] == "failed"
    assert res["verification"]["failure_kind"] == "worker_failure"
    assert "repair_handoff_requested" not in res
    assert "task" not in res
    assert res["progress_updates"][-1] == "verification failed"


def test_verify_result_surfaces_post_run_lint_warnings() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": ["workers/codex_cli_worker.py"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
                "budget_usage": {
                    "post_run_lint_format": {
                        "ran": True,
                        "errors": [
                            "`ruff check --fix -- workers/codex_cli_worker.py` exited with status 1"
                        ],
                    }
                },
            },
        }
    )

    res = verify_result(state)

    lint_check = next(
        item for item in res["verification"]["items"] if item["label"] == "post_run_lint_format"
    )
    assert lint_check["status"] == "warning"
    assert "reported 1 issue" in lint_check["message"]
    assert res["verification"]["status"] == "warning"


def test_verify_result_marks_post_run_lint_skip_as_passed() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": ["README.md"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
                "budget_usage": {"post_run_lint_format": {"ran": False, "status": "skipped"}},
            },
        }
    )

    res = verify_result(state)

    lint_check = next(
        item for item in res["verification"]["items"] if item["label"] == "post_run_lint_format"
    )
    assert lint_check["status"] == "passed"
    assert "skipped" in lint_check["message"]


def test_verify_result_queues_bounded_repair_handoff_after_verifier_failure() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "budget": {"max_retries": 1},
            },
            "task_spec": {"goal": "demo", "verification_commands": ["pytest -q tests/unit"]},
            "result": {
                "status": "success",
                "summary": "Applied change set.",
                "files_changed": ["orchestrator/graph.py"],
                "test_results": [{"name": "test1", "status": "failed"}],
                "commands_run": [],
            },
        }
    )

    res = verify_result(state)

    assert res["verification"]["status"] == "failed"
    assert res["repair_handoff_requested"] is True
    assert "queued bounded repair handoff (1/1)" in res["progress_updates"][-1]
    constraints = res["task"]["constraints"]
    assert constraints["independent_verifier_repair_passes_used"] == 1
    repair_text = constraints["independent_verifier_repair_request"]
    assert "Apply targeted code fixes for failed verification checks." in repair_text
    assert "pytest -q tests/unit" in repair_text


def test_verify_result_verifier_repair_budget_is_decoupled_from_max_retries() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "budget": {"max_retries": 0, "max_verifier_passes": 1},
            },
            "result": {
                "status": "success",
                "summary": "Applied change set.",
                "files_changed": ["orchestrator/graph.py"],
                "test_results": [{"name": "test1", "status": "failed"}],
                "commands_run": [],
            },
        }
    )

    res = verify_result(state)

    assert res["verification"]["status"] == "failed"
    assert res["repair_handoff_requested"] is True
    constraints = res["task"]["constraints"]
    assert constraints["independent_verifier_repair_passes_used"] == 1


def test_verify_result_stops_after_bounded_repair_attempts() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "constraints": {
                    "independent_verifier_repair_request": "repair text",
                    "independent_verifier_repair_passes_used": 1,
                },
                "budget": {"max_retries": 1},
            },
            "result": {
                "status": "success",
                "summary": "Applied repair candidate.",
                "files_changed": ["orchestrator/graph.py"],
                "test_results": [{"name": "test1", "status": "failed"}],
                "commands_run": [],
            },
        }
    )

    res = verify_result(state)

    assert res["verification"]["status"] == "failed"
    assert "repair_handoff_requested" not in res
    assert "independent_verifier_repair_request" not in res["task"]["constraints"]
    assert res["task"]["constraints"]["independent_verifier_repair_passes_used"] == 1
    assert "verification failed after bounded repair attempts" in res["progress_updates"][-1]
    assert (
        "Verification is still failing after 1 bounded repair attempt" in res["result"]["summary"]
    )
    assert res["result"]["next_action_hint"] == "await_manual_follow_up"


def test_verify_result_cleans_verifier_repair_task_after_successful_follow_up() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "constraints": {
                    "independent_verifier_repair_request": "repair text",
                    "independent_verifier_repair_passes_used": 1,
                },
                "budget": {"max_retries": 1},
            },
            "result": {
                "status": "success",
                "summary": "Applied repair candidate.",
                "files_changed": ["orchestrator/graph.py"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
            },
        }
    )

    res = verify_result(state)

    assert res["verification"]["status"] == "passed"
    assert "repair_handoff_requested" not in res
    assert "result" not in res
    assert "independent_verifier_repair_request" not in res["task"]["constraints"]
    assert res["task"]["constraints"]["independent_verifier_repair_passes_used"] == 1
    assert "verification passed after bounded repair handoff" in res["progress_updates"][-1]


def test_verify_result_runs_independent_verifier_when_enabled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("demo\n", encoding="utf-8")

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "task_spec": {"goal": "demo", "verification_commands": ["ls"]},
            "result": {
                "status": "success",
                "files_changed": ["README.md"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
                "artifacts": [
                    {
                        "name": "workspace",
                        "uri": workspace.as_uri(),
                        "artifact_type": "workspace",
                    }
                ],
            },
        }
    )

    res = verify_result(
        state,
        enable_independent_verifier=True,
        independent_verifier_outcome=("passed", "independent verification passed"),
    )

    independent_check = next(
        item for item in res["verification"]["items"] if item["label"] == "independent_verifier"
    )
    assert independent_check["status"] == "passed"
    assert res["verification"]["status"] == "passed"


def test_verify_result_fails_when_independent_verifier_command_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "task_spec": {"goal": "demo", "verification_commands": ["ls does-not-exist"]},
            "result": {
                "status": "success",
                "files_changed": ["README.md"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
                "artifacts": [
                    {
                        "name": "workspace",
                        "uri": workspace.as_uri(),
                        "artifact_type": "workspace",
                    }
                ],
            },
        }
    )

    res = verify_result(
        state,
        enable_independent_verifier=True,
        independent_verifier_outcome=("failed", "independent verification failed"),
    )

    independent_check = next(
        item for item in res["verification"]["items"] if item["label"] == "independent_verifier"
    )
    assert independent_check["status"] == "failed"
    assert res["verification"]["status"] == "failed"
    assert res["verification"]["failure_kind"] == "test_regression"


def test_verify_result_warns_when_independent_verifier_command_is_unsafe(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "task_spec": {"goal": "demo", "verification_commands": ["rm -rf ."]},
            "result": {
                "status": "success",
                "files_changed": ["README.md"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
                "artifacts": [
                    {
                        "name": "workspace",
                        "uri": workspace.as_uri(),
                        "artifact_type": "workspace",
                    }
                ],
            },
        }
    )

    res = verify_result(
        state,
        enable_independent_verifier=True,
        independent_verifier_outcome=("warning", "independent verifier warned"),
    )

    independent_check = next(
        item for item in res["verification"]["items"] if item["label"] == "independent_verifier"
    )
    assert independent_check["status"] == "warning"
    assert res["verification"]["status"] == "warning"


def test_compute_route_profile_aware_filters_read_only_when_mutations_allowed() -> None:
    """Read-only profiles should be filtered out when the task allows mutations."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "fix helper"}, "task_kind": "implementation"}
    )
    # Only a read-only profile is available for codex.
    profiles = {
        "codex-read-only": WorkerProfile(
            name="codex-read-only",
            worker_type="codex",
            runtime_mode="tool_loop",
            mutation_policy="read_only",
            capability_tags=["execution"],
        )
    }
    route = _compute_route_decision(
        state,
        frozenset({"codex"}),
        available_profiles=profiles,
    )

    # Should NOT select the read-only profile because task is NOT read-only.
    assert route.chosen_worker == "codex"
    assert route.chosen_profile is None
    assert route.route_reason == "runtime_unavailable"


def test_compute_route_profile_aware_selects_read_only_when_constrained() -> None:
    """Read-only profiles should be selected when the task is constrained to read-only."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "investigate code", "constraints": {"read_only": True}}}
    )
    profiles = {
        "codex-read-only": WorkerProfile(
            name="codex-read-only",
            worker_type="codex",
            runtime_mode="tool_loop",
            mutation_policy="read_only",
            capability_tags=["execution"],
        )
    }
    route = _compute_route_decision(
        state,
        frozenset({"codex"}),
        available_profiles=profiles,
    )

    assert route.chosen_worker == "codex"
    assert route.chosen_profile == "codex-read-only"
    assert route.route_reason == "cheap_mechanical_change"
