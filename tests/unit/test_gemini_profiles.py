import os

from apps.api.task_service_factory import _build_default_worker_profiles


def test_gemini_specialized_profiles_enabled():
    """Verify that specialized Gemini profiles are created when native_agent mode is active."""
    # Setup environment for native_agent mode
    os.environ["CODE_AGENT_WORKER_PROFILES_ENABLED"] = "true"
    os.environ["GEMINI_RUNTIME_MODE"] = "native_agent"

    profiles = _build_default_worker_profiles(
        include_gemini=True,
        include_openrouter=False,
        codex_runtime_mode="tool_loop",
        gemini_runtime_mode="native_agent",
    )

    assert "gemini-native-planner" in profiles
    assert "gemini-native-reviewer" in profiles

    planner = profiles["gemini-native-planner"]
    assert planner.worker_type == "gemini"
    assert planner.runtime_mode == "native_agent"
    assert "planning" in planner.capability_tags

    reviewer = profiles["gemini-native-reviewer"]
    assert reviewer.worker_type == "gemini"
    assert reviewer.runtime_mode == "native_agent"
    assert "review" in reviewer.capability_tags


def test_gemini_specialized_profiles_disabled_in_tool_loop():
    """Verify that specialized Gemini profiles are NOT created when tool_loop mode is active."""
    profiles = _build_default_worker_profiles(
        include_gemini=True,
        include_openrouter=False,
        codex_runtime_mode="tool_loop",
        gemini_runtime_mode="tool_loop",
    )

    assert "gemini-native-planner" not in profiles
    assert "gemini-native-reviewer" not in profiles
