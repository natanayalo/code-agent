"""Runtime mode helpers shared by API and worker entrypoints."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

RUN_API_ENV_VAR: Final[str] = "CODE_AGENT_RUN_API"
RUN_WORKER_ENV_VAR: Final[str] = "CODE_AGENT_RUN_WORKER"
TEMPORAL_ONLY_CUTOVER_AT_ENV_VAR: Final[str] = "TEMPORAL_ONLY_CUTOVER_AT"


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


def temporal_only_cutover_at(environ: Mapping[str, str] | None = None) -> datetime | None:
    """Return the configured immutable UTC cutover timestamp, if valid."""
    resolved_env = os.environ if environ is None else environ
    raw_value = resolved_env.get(TEMPORAL_ONLY_CUTOVER_AT_ENV_VAR, "").strip()
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def validate_cutover_configuration(environ: Mapping[str, str] | None = None) -> None:
    """Fail startup visibly for invalid cutover deployment configuration."""
    resolved_env = os.environ if environ is None else environ
    raw_cutover = resolved_env.get(TEMPORAL_ONLY_CUTOVER_AT_ENV_VAR, "").strip()
    if raw_cutover and temporal_only_cutover_at(resolved_env) is None:
        raise ValueError(f"{TEMPORAL_ONLY_CUTOVER_AT_ENV_VAR} must be an aware ISO-8601 timestamp.")


def initialize_persisted_cutover(session_factory: Any) -> datetime | None:
    """Persist/read the immutable cutover record after database bootstrap."""
    from repositories import RuntimeCutoverRepository, session_scope

    with session_scope(session_factory) as session:
        return RuntimeCutoverRepository(session).initialize_temporal_only(
            temporal_only_cutover_at()
        )


def should_run_api(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether API runtime is enabled for this process."""
    resolved_env = os.environ if environ is None else environ
    return is_enabled(resolved_env.get(RUN_API_ENV_VAR), default=True)


def should_run_worker(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether worker runtime is enabled for this process."""
    resolved_env = os.environ if environ is None else environ
    return is_enabled(resolved_env.get(RUN_WORKER_ENV_VAR), default=False)
