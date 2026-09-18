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


def test_resolve_antigravity_compound_slug_env_default_effort() -> None:
    config = resolve_antigravity_model_config(
        env={"CODE_AGENT_ANTIGRAVITY_MODEL": "gemini-3.5-flash-low"}
    )
    assert config.model == "gemini-3.5-flash"
    assert config.reasoning_effort == "low"
    assert config.model_source == "environment"
    assert config.reasoning_effort_source == "environment"


def test_resolve_antigravity_compound_slug_cross_layer_precedence() -> None:
    # 1. Task model (high priority) + env effort (lower priority) -> task embedded effort wins
    cfg1 = resolve_antigravity_model_config(
        task_constraints={"antigravity_model": "gemini-3.8-flash-high"},
        env={"CODE_AGENT_ANTIGRAVITY_REASONING_EFFORT": "medium"},
    )
    assert cfg1.model == "gemini-3.8-flash"
    assert cfg1.reasoning_effort == "high"
    assert cfg1.model_source == "task_override"
    assert cfg1.reasoning_effort_source == "task_override"

    # 2. Env model (lower priority) + task effort (high priority) -> task independent effort wins
    cfg2 = resolve_antigravity_model_config(
        task_constraints={"antigravity_reasoning_effort": "low"},
        env={"CODE_AGENT_ANTIGRAVITY_MODEL": "gemini-3.8-flash-high"},
    )
    assert cfg2.model == "gemini-3.8-flash"
    assert cfg2.reasoning_effort == "low"
    assert cfg2.model_source == "environment"
    assert cfg2.reasoning_effort_source == "task_override"

    # 3. Same layer match -> accepted
    cfg3 = resolve_antigravity_model_config(
        task_constraints={
            "antigravity_model": "gemini-3.8-flash-high",
            "antigravity_reasoning_effort": "high",
        }
    )
    assert cfg3.model == "gemini-3.8-flash"
    assert cfg3.reasoning_effort == "high"
    assert cfg3.reasoning_effort_source == "task_override"

    # 4. Same layer conflict in env -> raises ValueError
    with pytest.raises(ValueError, match="Conflicting Antigravity model"):
        resolve_antigravity_model_config(
            env={
                "CODE_AGENT_ANTIGRAVITY_MODEL": "gemini-3.8-flash-high",
                "CODE_AGENT_ANTIGRAVITY_REASONING_EFFORT": "low",
            }
        )


def test_runtime_adapters_empty_env_provenance() -> None:
    from workers.antigravity_cli_adapter import AntigravityCliRuntimeAdapter
    from workers.codex_exec_adapter import CodexExecCliRuntimeAdapter

    codex_adapter = CodexExecCliRuntimeAdapter.from_env({})
    assert codex_adapter.model is None
    assert codex_adapter.reasoning_effort is None
    codex_cfg = resolve_codex_model_config(
        adapter_model=codex_adapter.model,
        adapter_reasoning_effort=codex_adapter.reasoning_effort,
        env=codex_adapter.env,
    )
    assert codex_cfg.model == DEFAULT_CODEX_MODEL
    assert codex_cfg.reasoning_effort == DEFAULT_CODEX_REASONING_EFFORT
    assert codex_cfg.model_source == "provider_default"
    assert codex_cfg.reasoning_effort_source == "provider_default"

    anti_adapter = AntigravityCliRuntimeAdapter.from_env({})
    assert anti_adapter.model is None
    assert anti_adapter.reasoning_effort is None
    anti_cfg = resolve_antigravity_model_config(
        adapter_model=anti_adapter.model,
        adapter_reasoning_effort=anti_adapter.reasoning_effort,
        env=anti_adapter.env,
    )
    assert anti_cfg.model == DEFAULT_ANTIGRAVITY_MODEL
    assert anti_cfg.reasoning_effort == DEFAULT_ANTIGRAVITY_REASONING_EFFORT
    assert anti_cfg.model_source == "provider_default"
    assert anti_cfg.reasoning_effort_source == "provider_default"


def test_runtime_adapters_explicit_env_provenance() -> None:
    from workers.antigravity_cli_adapter import AntigravityCliRuntimeAdapter
    from workers.codex_exec_adapter import CodexExecCliRuntimeAdapter

    codex_env = {
        "CODE_AGENT_CODEX_MODEL": "gpt-5.6-luna",
        "CODE_AGENT_CODEX_REASONING_EFFORT": "high",
    }
    codex_adapter = CodexExecCliRuntimeAdapter.from_env(codex_env)
    assert codex_adapter.model == "gpt-5.6-luna"
    assert codex_adapter.reasoning_effort == "high"
    codex_cfg = resolve_codex_model_config(
        adapter_model=codex_adapter.model,
        adapter_reasoning_effort=codex_adapter.reasoning_effort,
        env=codex_adapter.env,
    )
    assert codex_cfg.model == "gpt-5.6-luna"
    assert codex_cfg.reasoning_effort == "high"
    assert codex_cfg.model_source == "environment"
    assert codex_cfg.reasoning_effort_source == "environment"

    anti_env = {
        "CODE_AGENT_ANTIGRAVITY_MODEL": "gemini-3.8-flash",
        "CODE_AGENT_ANTIGRAVITY_REASONING_EFFORT": "medium",
    }
    anti_adapter = AntigravityCliRuntimeAdapter.from_env(anti_env)
    assert anti_adapter.model == "gemini-3.8-flash"
    assert anti_adapter.reasoning_effort == "medium"
    anti_cfg = resolve_antigravity_model_config(
        adapter_model=anti_adapter.model,
        adapter_reasoning_effort=anti_adapter.reasoning_effort,
        env=anti_adapter.env,
    )
    assert anti_cfg.model == "gemini-3.8-flash"
    assert anti_cfg.reasoning_effort == "medium"
    assert anti_cfg.model_source == "environment"
    assert anti_cfg.reasoning_effort_source == "environment"


def test_resolve_antigravity_rejects_invalid_effort() -> None:
    with pytest.raises(ValueError, match="Invalid Antigravity reasoning effort 'xhigh'"):
        resolve_antigravity_model_config(task_constraints={"antigravity_reasoning_effort": "xhigh"})

    with pytest.raises(ValueError, match="Invalid Antigravity reasoning effort 'ultra'"):
        resolve_antigravity_model_config(env={"CODE_AGENT_ANTIGRAVITY_REASONING_EFFORT": "ultra"})


def test_parse_antigravity_slug_unsupported_suffix() -> None:
    # 'xhigh' or 'max' are not recognized as Antigravity effort suffixes
    base, effort = parse_antigravity_model_slug("gemini-3.8-flash-xhigh")
    assert base == "gemini-3.8-flash-xhigh"
    assert effort is None
