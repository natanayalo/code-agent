# ruff: noqa: F403, F405
"""Routing-focused orchestrator graph unit tests."""

from __future__ import annotations

from tests.unit.orchestrator_graph_unit_support import *  # noqa: F403


def test_interaction_requirement_resolved_by_resume_token_fallback() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Need clarification",
                "constraints": {
                    "interactions": {
                        "old-hash": {
                            "status": "resolved",
                            "interaction_type": "clarification",
                            "data": {"resume_token": "clarification-task-1", "questions": ["old"]},
                        }
                    }
                },
            }
        }
    )
    assert _is_interaction_requirement_resolved(
        state,
        interaction_type="clarification",
        summary="Task requires clarification before execution can continue.",
        data={
            "source": "task_spec",
            "resume_token": "clarification-task-1",
            "questions": ["new wording"],
        },
    )


def test_interaction_requirement_not_resolved_without_resume_token() -> None:
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "Need clarification", "constraints": {"interactions": {}}}}
    )
    assert (
        _is_interaction_requirement_resolved(
            state,
            interaction_type="clarification",
            summary="Task requires clarification before execution can continue.",
            data={"source": "task_spec", "questions": ["q"]},
        )
        is False
    )


def test_interaction_requirement_ignores_invalid_interaction_shapes() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Need clarification",
                "constraints": {
                    "interactions": {
                        "x": "not-a-map",
                        "y": {
                            "status": "resolved",
                            "interaction_type": "clarification",
                            "data": "oops",
                        },
                    }
                },
            }
        }
    )
    assert (
        _is_interaction_requirement_resolved(
            state,
            interaction_type="clarification",
            summary="Task requires clarification before execution can continue.",
            data={"resume_token": "clarification-task-1"},
        )
        is False
    )


def test_deliverable_evidence_and_meaningful_deliverable_helpers() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix the bug", "constraints": {}},
            "task_spec": {"goal": "g", "task_type": "bugfix"},
        }
    )
    assert _requires_deliverable_evidence(state) is True
    assert _has_meaningful_deliverable(state) is False

    state.result = WorkerResult(status="success", summary="x" * 120, files_changed=[], artifacts=[])
    assert _has_meaningful_deliverable(state) is True


def test_await_clarification_returns_resolved_without_interrupt() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_id": "task-1",
                "task_text": "Need clarification",
                "constraints": {
                    "interactions": {
                        "resolved-hash": {
                            "status": "resolved",
                            "interaction_type": "clarification",
                            "data": {"resume_token": "clarification-task-1"},
                        },
                        "noise": "not-a-map",
                    }
                },
            },
            "task_spec": {
                "goal": "g",
                "requires_clarification": True,
                "clarification_questions": ["new question wording"],
            },
        }
    )
    result = await_clarification(state)
    assert result["current_step"] == "await_clarification"
    assert "clarification already resolved" in result["progress_updates"][-1]


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
    assert route.route_reason == "verifier_failed_previous_run"


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
    """T-071: escalation needed but alternate unavailable -> explicit failure, not blind retry."""
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
    route = _compute_route_decision(state, _CODEX_ONLY)
    assert route.chosen_worker == "gemini"
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


def test_compute_route_neither_worker_available():
    """_route_by_preference keeps the preferred intent when neither worker is available."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "task_kind": "architecture"}
    )
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
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "cheap_mechanical_change"


def test_compute_route_profile_aware_allows_read_only_when_mutations_allowed() -> None:
    """Read-only profiles remain routable even when task is not explicitly read-only."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "fix helper"}, "task_kind": "implementation"}
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


def test_compute_route_profile_aware_keeps_shell_smoke_on_normal_executor() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Smoke test: print PWD and HOME only, then exit."},
            "task_spec": {
                "goal": "Smoke test: print PWD and HOME only, then exit.",
                "allowed_actions": ["read_repo_files", "run_non_destructive_checks"],
                "delivery_mode": "summary",
            },
        }
    )
    profiles = {
        "codex-native-executor": WorkerProfile(
            name="codex-native-executor",
            worker_type="codex",
            runtime_mode="native_agent",
            mutation_policy="patch_allowed",
            capability_tags=["execution"],
            supported_delivery_modes=["workspace", "branch", "draft_pr"],
        ),
        "codex-native-executor-read-only": WorkerProfile(
            name="codex-native-executor-read-only",
            worker_type="codex",
            runtime_mode="native_agent",
            mutation_policy="read_only",
            capability_tags=["execution"],
            supported_delivery_modes=["workspace", "branch", "draft_pr"],
        ),
    }

    route = _compute_route_decision(
        state,
        frozenset({"codex"}),
        available_profiles=profiles,
    )

    assert route.chosen_worker == "codex"
    assert route.chosen_profile == "codex-native-executor"
    assert route.route_reason == "cheap_mechanical_change"
