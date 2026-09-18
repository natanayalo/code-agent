"""Unit tests for model and reasoning effort resolution."""

from __future__ import annotations

import pytest

from workers.base import ModelExecutionMetadata
from workers.model_config import (
    DEFAULT_ANTIGRAVITY_MODEL,
    DEFAULT_ANTIGRAVITY_REASONING_EFFORT,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_REASONING_EFFORT,
    build_antigravity_model_cli_args,
    build_codex_model_cli_args,
    parse_antigravity_model_slug,
    resolve_antigravity_model_config,
    resolve_codex_model_config,
)


def test_parse_antigravity_model_slug() -> None:
    assert parse_antigravity_model_slug("gemini-3.8-flash-medium") == (
        "gemini-3.8-flash",
        "medium",
    )
    assert parse_antigravity_model_slug("gemini-3.8-flash-HIGH") == (
        "gemini-3.8-flash",
        "high",
    )
    assert parse_antigravity_model_slug("gemini-3.8-flash") == (
        "gemini-3.8-flash",
        None,
    )
    assert parse_antigravity_model_slug("auto-gemini-2.5") == (
        "auto-gemini-2.5",
        None,
    )


def test_resolve_codex_model_config_provider_defaults() -> None:
    config = resolve_codex_model_config()
    assert config.provider == "codex"
    assert config.model == DEFAULT_CODEX_MODEL
    assert config.reasoning_effort == DEFAULT_CODEX_REASONING_EFFORT
    assert config.model_source == "provider_default"
    assert config.reasoning_effort_source == "provider_default"
    assert config.requested_model is None
    assert config.requested_reasoning_effort is None


def test_resolve_codex_model_config_precedence() -> None:
    env = {
        "CODE_AGENT_CODEX_MODEL": "gpt-5.5",
        "CODE_AGENT_CODEX_REASONING_EFFORT": "low",
    }
    cfg_env = resolve_codex_model_config(env=env)
    assert cfg_env.model == "gpt-5.5"
    assert cfg_env.model_source == "environment"
    assert cfg_env.reasoning_effort == "low"
    assert cfg_env.reasoning_effort_source == "environment"

    cfg_adapter = resolve_codex_model_config(
        adapter_model="gpt-5.4",
        adapter_reasoning_effort="medium",
        env=env,
    )
    assert cfg_adapter.model == "gpt-5.4"
    assert cfg_adapter.model_source == "environment"
    assert cfg_adapter.reasoning_effort == "medium"
    assert cfg_adapter.reasoning_effort_source == "environment"

    cfg_profile = resolve_codex_model_config(
        manifest_worker={"model": "gpt-5.3", "reasoning_effort": "high"},
        adapter_model="gpt-5.4",
        env=env,
    )
    assert cfg_profile.model == "gpt-5.3"
    assert cfg_profile.model_source == "worker_profile"
    assert cfg_profile.reasoning_effort == "high"
    assert cfg_profile.reasoning_effort_source == "worker_profile"

    cfg_task = resolve_codex_model_config(
        task_constraints={"codex_model": "gpt-5.6", "codex_reasoning_effort": "xhigh"},
        manifest_worker={"model": "gpt-5.3", "reasoning_effort": "high"},
        env=env,
    )
    assert cfg_task.model == "gpt-5.6"
    assert cfg_task.model_source == "task_override"
    assert cfg_task.reasoning_effort == "xhigh"
    assert cfg_task.reasoning_effort_source == "task_override"
    assert cfg_task.requested_model == "gpt-5.6"
    assert cfg_task.requested_reasoning_effort == "xhigh"


def test_resolve_codex_model_config_independent_provenance() -> None:
    config = resolve_codex_model_config(
        task_constraints={"reasoning_effort": "low"},
        env={"CODE_AGENT_CODEX_MODEL": "gpt-5.5"},
    )
    assert config.model == "gpt-5.5"
    assert config.model_source == "environment"
    assert config.requested_model is None
    assert config.reasoning_effort == "low"
    assert config.reasoning_effort_source == "task_override"
    assert config.requested_reasoning_effort == "low"


def test_build_codex_model_cli_args() -> None:
    config = resolve_codex_model_config()
    args = build_codex_model_cli_args(config)
    assert args == [
        "--model",
        DEFAULT_CODEX_MODEL,
        "-c",
        f'model_reasoning_effort="{DEFAULT_CODEX_REASONING_EFFORT}"',
    ]


def test_resolve_antigravity_model_config_provider_defaults() -> None:
    config = resolve_antigravity_model_config()
    assert config.provider == "antigravity"
    assert config.model == DEFAULT_ANTIGRAVITY_MODEL
    assert config.reasoning_effort == DEFAULT_ANTIGRAVITY_REASONING_EFFORT
    assert config.model_source == "provider_default"
    assert config.reasoning_effort_source == "provider_default"
    assert config.requested_model is None
    assert config.requested_reasoning_effort is None


def test_resolve_antigravity_model_config_precedence() -> None:
    env = {
        "CODE_AGENT_ANTIGRAVITY_MODEL": "gemini-3-pro",
        "CODE_AGENT_ANTIGRAVITY_REASONING_EFFORT": "low",
    }
    cfg_env = resolve_antigravity_model_config(env=env)
    assert cfg_env.model == "gemini-3-pro"
    assert cfg_env.model_source == "environment"
    assert cfg_env.reasoning_effort == "low"
    assert cfg_env.reasoning_effort_source == "environment"

    cfg_profile = resolve_antigravity_model_config(
        manifest_worker={"model": "gemini-3.8-flash", "reasoning_effort": "high"},
        env=env,
    )
    assert cfg_profile.model == "gemini-3.8-flash"
    assert cfg_profile.model_source == "worker_profile"
    assert cfg_profile.reasoning_effort == "high"
    assert cfg_profile.reasoning_effort_source == "worker_profile"

    cfg_task = resolve_antigravity_model_config(
        task_constraints={
            "antigravity_model": "gemini-3.8-flash",
            "antigravity_reasoning_effort": "medium",
        },
        manifest_worker={"model": "gemini-3.8-flash", "reasoning_effort": "high"},
        env=env,
    )
    assert cfg_task.model == "gemini-3.8-flash"
    assert cfg_task.model_source == "task_override"
    assert cfg_task.reasoning_effort == "medium"
    assert cfg_task.reasoning_effort_source == "task_override"
    assert cfg_task.requested_model == "gemini-3.8-flash"
    assert cfg_task.requested_reasoning_effort == "medium"


def test_resolve_antigravity_compound_slug_decomposition() -> None:
    config = resolve_antigravity_model_config(
        task_constraints={"model": "gemini-3.8-flash-high", "worker_type": "antigravity"}
    )
    assert config.model == "gemini-3.8-flash"
    assert config.reasoning_effort == "high"
    assert config.model_source == "task_override"
    assert config.reasoning_effort_source == "task_override"
    assert config.requested_model == "gemini-3.8-flash-high"
    assert config.requested_reasoning_effort is None


def test_resolve_antigravity_contradiction_raises() -> None:
    with pytest.raises(ValueError, match="Conflicting Antigravity model"):
        resolve_antigravity_model_config(
            task_constraints={
                "antigravity_model": "gemini-3.8-flash-high",
                "antigravity_reasoning_effort": "low",
            }
        )


def test_build_antigravity_model_cli_args() -> None:
    config = resolve_antigravity_model_config()
    args = build_antigravity_model_cli_args(config)
    assert args == [
        "--model",
        DEFAULT_ANTIGRAVITY_MODEL,
        "--effort",
        DEFAULT_ANTIGRAVITY_REASONING_EFFORT,
    ]

    auto_config = resolve_antigravity_model_config(
        task_constraints={"antigravity_model": "auto-gemini-2.5"}
    )
    assert build_antigravity_model_cli_args(auto_config) == []


def test_config_to_metadata_export() -> None:
    config = resolve_codex_model_config(
        task_constraints={"codex_reasoning_effort": "medium"},
        env={"CODE_AGENT_CODEX_MODEL": "gpt-5.6-luna"},
    )
    meta = config.to_metadata()
    assert isinstance(meta, ModelExecutionMetadata)
    assert meta.provider == "codex"
    assert meta.model == "gpt-5.6-luna"
    assert meta.reasoning_effort == "medium"
    assert meta.model_source == "environment"
    assert meta.reasoning_effort_source == "task_override"
    assert meta.requested_model is None
    assert meta.requested_reasoning_effort == "medium"
