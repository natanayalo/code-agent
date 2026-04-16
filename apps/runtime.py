"""Runtime mode helpers shared by API and worker entrypoints."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

RUN_API_ENV_VAR: Final[str] = "CODE_AGENT_RUN_API"
RUN_WORKER_ENV_VAR: Final[str] = "CODE_AGENT_RUN_WORKER"


def _is_enabled(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


def should_run_api(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether API runtime is enabled for this process."""
    resolved_env = os.environ if environ is None else environ
    return _is_enabled(resolved_env.get(RUN_API_ENV_VAR), default=True)


def should_run_worker(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether worker runtime is enabled for this process."""
    resolved_env = os.environ if environ is None else environ
    return _is_enabled(resolved_env.get(RUN_WORKER_ENV_VAR), default=False)
