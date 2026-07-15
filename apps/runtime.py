"""Runtime mode helpers shared by API and worker entrypoints."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

RUN_API_ENV_VAR: Final[str] = "CODE_AGENT_RUN_API"
RUN_WORKER_ENV_VAR: Final[str] = "CODE_AGENT_RUN_WORKER"
EXECUTION_RUNTIME_ENV_VAR: Final[str] = "CODE_AGENT_EXECUTION_RUNTIME"
TEMPORAL_EXECUTION_RUNTIME: Final[str] = "temporal"
LEGACY_EXECUTION_RUNTIME: Final[str] = "legacy"
_LEGACY_TEMPORAL_FLAG_ENV_VAR: Final[str] = "CODE_AGENT_USE_TEMPORAL"


def is_enabled(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Compatibility for callers that used the original private helper. New code
# should use is_enabled so feature flags have one shared interpretation.
_is_enabled = is_enabled


def coerce_positive_int_env(value: str | None, *, default: int) -> int:
    """Parse positive integer env settings with a safe fallback."""
    if value is None:
        return default
    stripped = value.strip()
    if not stripped:
        return default
    try:
        parsed = int(stripped)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def coerce_non_negative_int_env(value: str | None, *, default: int) -> int:
    """Parse non-negative integer env settings with a safe fallback."""
    if value is None:
        return default
    stripped = value.strip()
    if not stripped:
        return default
    try:
        parsed = int(stripped)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def execution_runtime(environ: Mapping[str, str] | None = None) -> str:
    """Return the selected execution runtime with a safe legacy fallback.

    ``CODE_AGENT_EXECUTION_RUNTIME`` is the explicit selector. The former
    ``CODE_AGENT_USE_TEMPORAL`` boolean remains supported while deployments
    transition, but the explicit selector wins whenever it is valid.
    """
    resolved_env = os.environ if environ is None else environ
    selected = resolved_env.get(EXECUTION_RUNTIME_ENV_VAR, "").strip().lower()
    if selected in {TEMPORAL_EXECUTION_RUNTIME, LEGACY_EXECUTION_RUNTIME}:
        return selected
    if is_enabled(resolved_env.get(_LEGACY_TEMPORAL_FLAG_ENV_VAR), default=False):
        return TEMPORAL_EXECUTION_RUNTIME
    return LEGACY_EXECUTION_RUNTIME


def uses_temporal_execution(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the selected execution runtime is Temporal."""
    return execution_runtime(environ) == TEMPORAL_EXECUTION_RUNTIME


def should_run_api(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether API runtime is enabled for this process."""
    resolved_env = os.environ if environ is None else environ
    return is_enabled(resolved_env.get(RUN_API_ENV_VAR), default=True)


def should_run_worker(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether worker runtime is enabled for this process."""
    resolved_env = os.environ if environ is None else environ
    return is_enabled(resolved_env.get(RUN_WORKER_ENV_VAR), default=False)
