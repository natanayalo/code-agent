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
        codex_runtime_mode=WorkerRuntimeMode.TOOL_LOOP,
        gemini_runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
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


def test_gemini_specialized_profiles_disabled_in_tool_loop():
    """Verify that specialized Gemini profiles are NOT created when tool_loop mode is active."""
    profiles = _build_default_worker_profiles(
        include_gemini=True,
        include_openrouter=False,
        codex_runtime_mode=WorkerRuntimeMode.TOOL_LOOP,
        gemini_runtime_mode=WorkerRuntimeMode.TOOL_LOOP,
    )

    assert GEMINI_NATIVE_PLANNER_PROFILE not in profiles
    assert GEMINI_NATIVE_REVIEWER_PROFILE not in profiles


def test_invalid_runtime_mode_raises_error():
    """Verify that an invalid runtime mode explicitly provided raises ValueError."""
    with pytest.raises(ValueError, match="Invalid worker runtime mode: 'invalid_mode'"):
        _coerce_runtime_mode("invalid_mode", default=WorkerRuntimeMode.TOOL_LOOP)
