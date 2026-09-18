"""Model and reasoning effort configuration resolution across coding workers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from workers.base import ConfigSource, ModelExecutionMetadata

DEFAULT_CODEX_MODEL: Final[str] = "gpt-5.6-luna"
DEFAULT_CODEX_REASONING_EFFORT: Final[str] = "high"

DEFAULT_ANTIGRAVITY_MODEL: Final[str] = "gemini-3.8-flash"
DEFAULT_ANTIGRAVITY_REASONING_EFFORT: Final[str] = "medium"

CODEX_MODEL_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_MODEL"
CODEX_REASONING_EFFORT_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_REASONING_EFFORT"

ANTIGRAVITY_MODEL_ENV_VAR: Final[str] = "CODE_AGENT_ANTIGRAVITY_MODEL"
ANTIGRAVITY_REASONING_EFFORT_ENV_VAR: Final[str] = "CODE_AGENT_ANTIGRAVITY_REASONING_EFFORT"

ANTIGRAVITY_REASONING_EFFORTS: Final[frozenset[str]] = frozenset({"low", "medium", "high"})

_EFFORT_SLUG_SUFFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<base>.+)-(?P<effort>low|medium|high)$", re.IGNORECASE
)


@dataclass(frozen=True)
class ResolvedModelConfig:
    """Resolved model and effort configuration carrying independent per-field provenance."""

    provider: str
    model: str
    reasoning_effort: str | None
    requested_model: str | None
    requested_reasoning_effort: str | None
    model_source: ConfigSource
    reasoning_effort_source: ConfigSource | None

    def to_metadata(self) -> ModelExecutionMetadata:
        """Export as the authoritative typed ModelExecutionMetadata contract."""
        return ModelExecutionMetadata(
            provider=self.provider,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            requested_model=self.requested_model,
            requested_reasoning_effort=self.requested_reasoning_effort,
            model_source=self.model_source,
            reasoning_effort_source=self.reasoning_effort_source,
        )


def _clean_str(value: Any | None) -> str | None:
    """Strip and normalize text values; return None for empty or blank values."""
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return str(value).strip() or None


def _clean_effort(value: Any | None) -> str | None:
    """Strip and lowercase reasoning effort values."""
    cleaned = _clean_str(value)
    return cleaned.lower() if cleaned is not None else None


def parse_antigravity_model_slug(raw_model: str) -> tuple[str, str | None]:
    """Decompose an Antigravity model slug into base family and embedded effort if present.

    Example:
        `gemini-3.8-flash-medium` -> (`gemini-3.8-flash`, `medium`)
        `gemini-3.8-flash` -> (`gemini-3.8-flash`, None)
        `auto-gemini-2.5` -> (`auto-gemini-2.5`, None)
    """
    cleaned = raw_model.strip()
    match = _EFFORT_SLUG_SUFFIX_PATTERN.match(cleaned)
    if match:
        base = match.group("base").strip()
        effort = match.group("effort").lower()
        return base, effort
    return cleaned, None


def _resolve_codex_model(
    *,
    requested_model: str | None,
    manifest: Mapping[str, Any],
    adapter_model: str | None,
    resolved_env: Mapping[str, str],
    default_model: str,
) -> tuple[str, ConfigSource]:
    """Resolve effective Codex model and provenance."""
    if requested_model is not None:
        return requested_model, "task_override"
    if (manifest_model := _clean_str(manifest.get("model"))) is not None:
        return manifest_model, "worker_profile"
    if adapter_model is not None and _clean_str(adapter_model) is not None:
        return _clean_str(adapter_model) or default_model, "environment"
    if (env_model := _clean_str(resolved_env.get(CODEX_MODEL_ENV_VAR))) is not None:
        return env_model, "environment"
    return default_model, "provider_default"


def _resolve_codex_effort(
    *,
    requested_effort: str | None,
    manifest: Mapping[str, Any],
    adapter_effort: str | None,
    resolved_env: Mapping[str, str],
    default_effort: str,
) -> tuple[str | None, ConfigSource | None]:
    """Resolve effective Codex reasoning effort and provenance."""
    if requested_effort is not None:
        return requested_effort, "task_override"
    if (manifest_effort := _clean_effort(manifest.get("reasoning_effort"))) is not None:
        return manifest_effort, "worker_profile"
    if adapter_effort is not None and _clean_effort(adapter_effort) is not None:
        return _clean_effort(adapter_effort), "environment"
    if (env_effort := _clean_effort(resolved_env.get(CODEX_REASONING_EFFORT_ENV_VAR))) is not None:
        return env_effort, "environment"
    return default_effort, "provider_default"


def resolve_codex_model_config(
    *,
    task_constraints: Mapping[str, Any] | None = None,
    manifest_worker: Mapping[str, Any] | None = None,
    adapter_model: str | None = None,
    adapter_reasoning_effort: str | None = None,
    env: Mapping[str, str] | None = None,
    default_model: str = DEFAULT_CODEX_MODEL,
    default_reasoning_effort: str = DEFAULT_CODEX_REASONING_EFFORT,
) -> ResolvedModelConfig:
    """Resolve Codex model and effort following strict precedence:

    task constraint > manifest profile > adapter/env > provider default.
    """
    constraints = dict(task_constraints or {})
    manifest = dict(manifest_worker or {})
    resolved_env = dict(env or {})

    requested_model = _clean_str(
        constraints.get("codex_model")
        or (constraints.get("model") if constraints.get("worker_type") in (None, "codex") else None)
    )
    requested_effort = _clean_effort(
        constraints.get("codex_reasoning_effort") or constraints.get("reasoning_effort")
    )

    effective_model, model_source = _resolve_codex_model(
        requested_model=requested_model,
        manifest=manifest,
        adapter_model=adapter_model,
        resolved_env=resolved_env,
        default_model=default_model,
    )
    effective_effort, effort_source = _resolve_codex_effort(
        requested_effort=requested_effort,
        manifest=manifest,
        adapter_effort=adapter_reasoning_effort,
        resolved_env=resolved_env,
        default_effort=default_reasoning_effort,
    )

    return ResolvedModelConfig(
        provider="codex",
        model=effective_model,
        reasoning_effort=effective_effort,
        requested_model=requested_model,
        requested_reasoning_effort=requested_effort,
        model_source=model_source,
        reasoning_effort_source=effort_source,
    )


def build_codex_model_cli_args(config: ResolvedModelConfig) -> list[str]:
    """Emit the single canonical argv slice for Codex model and effort."""
    args: list[str] = []
    if config.model:
        args.extend(["--model", config.model])
    if config.reasoning_effort:
        args.extend(["-c", f'model_reasoning_effort="{config.reasoning_effort}"'])
    return args


def _resolve_raw_antigravity_model(
    *,
    raw_requested_model: str | None,
    manifest: Mapping[str, Any],
    adapter_model: str | None,
    resolved_env: Mapping[str, str],
    default_model: str,
) -> tuple[str, ConfigSource]:
    """Resolve raw Antigravity model string and provenance."""
    if raw_requested_model is not None:
        return raw_requested_model, "task_override"
    if (manifest_model := _clean_str(manifest.get("model"))) is not None:
        return manifest_model, "worker_profile"
    if adapter_model is not None and _clean_str(adapter_model) is not None:
        return _clean_str(adapter_model) or default_model, "environment"
    if (
        env_model := _clean_str(
            resolved_env.get(ANTIGRAVITY_MODEL_ENV_VAR)
            or resolved_env.get("CODE_AGENT_GEMINI_MODEL")
        )
    ) is not None:
        return env_model, "environment"
    return default_model, "provider_default"


_SOURCE_PRECEDENCE: Final[dict[ConfigSource, int]] = {
    "task_override": 4,
    "worker_profile": 3,
    "environment": 2,
    "provider_default": 1,
}


def _resolve_independent_antigravity_effort(
    *,
    requested_effort: str | None,
    manifest: Mapping[str, Any],
    adapter_effort: str | None,
    resolved_env: Mapping[str, str],
) -> tuple[str | None, ConfigSource | None]:
    """Find the highest-precedence independently configured effort, if any."""
    if requested_effort is not None:
        return requested_effort, "task_override"
    if (manifest_effort := _clean_effort(manifest.get("reasoning_effort"))) is not None:
        return manifest_effort, "worker_profile"
    if adapter_effort is not None and _clean_effort(adapter_effort) is not None:
        return _clean_effort(adapter_effort), "environment"
    if (
        env_effort := _clean_effort(
            resolved_env.get(ANTIGRAVITY_REASONING_EFFORT_ENV_VAR)
            or resolved_env.get("CODE_AGENT_GEMINI_REASONING_EFFORT")
        )
    ) is not None:
        return env_effort, "environment"
    return None, None


def _resolve_raw_antigravity_effort(
    *,
    requested_effort: str | None,
    manifest: Mapping[str, Any],
    adapter_effort: str | None,
    resolved_env: Mapping[str, str],
    embedded_effort: str | None,
    model_source: ConfigSource,
    default_effort: str,
    raw_model: str,
    base_family: str,
) -> tuple[str | None, ConfigSource | None]:
    """Resolve raw Antigravity effort using layered source precedence."""
    ind_effort, ind_source = _resolve_independent_antigravity_effort(
        requested_effort=requested_effort,
        manifest=manifest,
        adapter_effort=adapter_effort,
        resolved_env=resolved_env,
    )
    if ind_effort is not None and ind_effort not in ANTIGRAVITY_REASONING_EFFORTS:
        allowed = ", ".join(sorted(ANTIGRAVITY_REASONING_EFFORTS))
        raise ValueError(
            f"Invalid Antigravity reasoning effort '{ind_effort}'. Allowed efforts: {allowed}."
        )

    if ind_effort is not None and embedded_effort is not None:
        assert ind_source is not None
        ind_prec = _SOURCE_PRECEDENCE[ind_source]
        model_prec = _SOURCE_PRECEDENCE[model_source]
        if ind_prec > model_prec:
            return ind_effort, ind_source
        if model_prec > ind_prec:
            return embedded_effort, model_source
        if ind_effort != embedded_effort:
            raise ValueError(
                f"Conflicting Antigravity model '{raw_model}' "
                f"(specifies effort '{embedded_effort}') "
                f"and {ind_source} reasoning effort '{ind_effort}'. "
                f"Specify base model family '{base_family}' or matching effort."
            )
        return ind_effort, ind_source

    if ind_effort is not None:
        return ind_effort, ind_source
    if embedded_effort is not None:
        return embedded_effort, model_source
    return default_effort, "provider_default"


def resolve_antigravity_model_config(
    *,
    task_constraints: Mapping[str, Any] | None = None,
    manifest_worker: Mapping[str, Any] | None = None,
    adapter_model: str | None = None,
    adapter_reasoning_effort: str | None = None,
    env: Mapping[str, str] | None = None,
    default_model: str = DEFAULT_ANTIGRAVITY_MODEL,
    default_reasoning_effort: str = DEFAULT_ANTIGRAVITY_REASONING_EFFORT,
) -> ResolvedModelConfig:
    """Resolve Antigravity model and effort following strict precedence:

    task constraint > manifest profile > adapter/env > provider default.
    Normalizes compound slugs (e.g. `gemini-3.8-flash-medium` -> `gemini-3.8-flash` + `medium`)
    and enforces precedence-aware conflict resolution.
    """
    constraints = dict(task_constraints or {})
    manifest = dict(manifest_worker or {})
    resolved_env = dict(env or {})

    raw_requested_model = _clean_str(
        constraints.get("antigravity_model")
        or (
            constraints.get("model")
            if constraints.get("worker_type") in (None, "antigravity")
            else None
        )
    )
    requested_effort = _clean_effort(
        constraints.get("antigravity_reasoning_effort") or constraints.get("reasoning_effort")
    )

    raw_model, model_source = _resolve_raw_antigravity_model(
        raw_requested_model=raw_requested_model,
        manifest=manifest,
        adapter_model=adapter_model,
        resolved_env=resolved_env,
        default_model=default_model,
    )
    base_family, embedded_effort = parse_antigravity_model_slug(raw_model)
    if base_family.lower().startswith("auto-"):
        raise ValueError(
            "Antigravity auto-* routing aliases cannot be used as execution models; "
            "specify an explicit model returned by `agy models`."
        )

    effective_effort, effort_source = _resolve_raw_antigravity_effort(
        requested_effort=requested_effort,
        manifest=manifest,
        adapter_effort=adapter_reasoning_effort,
        resolved_env=resolved_env,
        embedded_effort=embedded_effort,
        model_source=model_source,
        default_effort=default_reasoning_effort,
        raw_model=raw_model,
        base_family=base_family,
    )

    return ResolvedModelConfig(
        provider="antigravity",
        model=base_family,
        reasoning_effort=effective_effort,
        requested_model=raw_requested_model,
        requested_reasoning_effort=requested_effort,
        model_source=model_source,
        reasoning_effort_source=effort_source,
    )


def build_antigravity_model_cli_args(config: ResolvedModelConfig) -> list[str]:
    """Emit the single canonical argv slice for Antigravity model and effort."""
    args: list[str] = []
    if config.model:
        args.extend(["--model", config.model])
        if config.reasoning_effort:
            args.extend(["--effort", config.reasoning_effort])
    return args
