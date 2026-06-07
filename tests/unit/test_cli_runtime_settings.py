# ruff: noqa: F403, F405
"""Behavior-focused CLI runtime tests."""

from __future__ import annotations

from tests.unit.cli_runtime_support import *  # noqa: F403


def test_settings_from_budget_applies_supported_runtime_overrides() -> None:
    """Budget fields should override the inner-loop defaults we support today."""
    settings = settings_from_budget(
        {
            "max_iterations": "12",
            "max_minutes": 2,
            "command_timeout_seconds": 9,
            "max_tool_calls": "5",
            "max_shell_commands": 6,
            "max_retries": 0,
            "max_verifier_passes": "1",
            "max_exploration_iterations": 4,
            "max_execution_iterations": "6",
            "stall_window_iterations": 5,
            "max_repeated_file_reads": "7",
            "stall_correction_turns": 2,
            "max_observation_characters": 512,
            "context_window_limit_tokens": "64000",
        },
        defaults=CliRuntimeSettings(max_iterations=4, worker_timeout_seconds=30),
    )

    assert settings.max_iterations == 12
    assert settings.worker_timeout_seconds == 120
    assert settings.command_timeout_seconds == 9
    assert settings.max_tool_calls == 5
    assert settings.max_shell_commands == 6
    assert settings.max_retries == 0
    assert settings.max_verifier_passes == 1
    assert settings.max_exploration_iterations == 4
    assert settings.max_execution_iterations == 6
    assert settings.stall_window_iterations == 5
    assert settings.max_repeated_file_reads == 7
    assert settings.stall_correction_turns == 2
    assert settings.max_observation_characters == 512
    assert settings.context_window_limit_tokens == 64000


def test_runtime_settings_default_worker_timeout_is_600_seconds() -> None:
    """Native execution default timeout should be increased to 600 seconds."""
    settings = CliRuntimeSettings()
    assert settings.worker_timeout_seconds == 600


def test_settings_from_budget_accepts_fractional_numeric_strings_like_float_inputs() -> None:
    """Numeric strings should be coerced with the same truncation behavior as float inputs."""
    settings = settings_from_budget(
        {
            "max_iterations": "2.5",
            "command_timeout_seconds": "9.9",
        },
        defaults=CliRuntimeSettings(max_iterations=4, command_timeout_seconds=30),
    )

    assert settings.max_iterations == 2
    assert settings.command_timeout_seconds == 9


def test_settings_from_budget_accepts_zero_for_tool_and_shell_limits() -> None:
    """Zero should be a valid explicit limit for tool/shell execution budgets."""
    settings = settings_from_budget(
        {
            "max_tool_calls": 0,
            "max_shell_commands": "0",
        },
        defaults=CliRuntimeSettings(max_tool_calls=None, max_shell_commands=None),
    )

    assert settings.max_tool_calls == 0
    assert settings.max_shell_commands == 0


def test_coerce_non_negative_int_rejects_non_finite_floats() -> None:
    """NaN and infinity should be ignored instead of crashing runtime budget parsing."""
    assert _coerce_non_negative_int(float("nan")) is None
    assert _coerce_non_negative_int(float("inf")) is None
    assert _coerce_non_negative_int(float("-inf")) is None


def test_format_bash_observation_truncates_long_output() -> None:
    """Shell observations should stay bounded and call out truncation explicitly."""
    observation = format_bash_observation(
        _command_result("cat README.md", output="abcdefghij"),
        max_characters=5,
    )

    assert "Command: cat README.md" in observation
    assert "Exit code: 0" in observation
    assert "[truncated]...fghij" in observation
