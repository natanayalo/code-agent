import pytest

from apps.api.task_service_factory import (
    GEMINI_NATIVE_PLANNER_PROFILE,
    GEMINI_NATIVE_REVIEWER_PROFILE,
    WorkerRuntimeMode,
    _build_default_worker_profiles,
    _coerce_runtime_mode,
)


def test_gemini_specialized_profiles_enabled(monkeypatch):
    """Verify that specialized Gemini profiles are created when native_agent mode is active."""
    # Setup environment for native_agent mode
    monkeypatch.setenv("CODE_AGENT_WORKER_PROFILES_ENABLED", "true")
    monkeypatch.setenv("GEMINI_RUNTIME_MODE", "native_agent")

    profiles = _build_default_worker_profiles(
        include_gemini=True,
        include_openrouter=False,
        include_codex_legacy_tool_loop=False,
        include_gemini_legacy_tool_loop=False,
    )

    assert GEMINI_NATIVE_PLANNER_PROFILE in profiles
    assert GEMINI_NATIVE_REVIEWER_PROFILE in profiles

    planner = profiles[GEMINI_NATIVE_PLANNER_PROFILE]
    assert planner.worker_type == "gemini"
    assert planner.runtime_mode == WorkerRuntimeMode.PLANNER_ONLY
    assert "planning" in planner.capability_tags

    reviewer = profiles[GEMINI_NATIVE_REVIEWER_PROFILE]
    assert reviewer.worker_type == "gemini"
    assert reviewer.runtime_mode == WorkerRuntimeMode.REVIEWER_ONLY
    assert "review" in reviewer.capability_tags


def test_gemini_specialized_profiles_stay_enabled_with_legacy_tool_loop_opt_in():
    """Gemini planner/reviewer profiles should remain available even with legacy opt-in."""
    profiles = _build_default_worker_profiles(
        include_gemini=True,
        include_openrouter=False,
        include_codex_legacy_tool_loop=False,
        include_gemini_legacy_tool_loop=True,
    )

    assert GEMINI_NATIVE_PLANNER_PROFILE in profiles
    assert GEMINI_NATIVE_REVIEWER_PROFILE in profiles
    assert "gemini-tool-loop-executor" in profiles
    assert profiles["gemini-tool-loop-executor"].runtime_mode == WorkerRuntimeMode.TOOL_LOOP


def test_invalid_runtime_mode_raises_error():
    """Verify that an invalid runtime mode explicitly provided raises ValueError."""
    with pytest.raises(ValueError, match="Invalid worker runtime mode: 'invalid_mode'"):
        _coerce_runtime_mode("invalid_mode", default=WorkerRuntimeMode.TOOL_LOOP)
