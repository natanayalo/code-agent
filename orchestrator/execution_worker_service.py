"""Worker-node registry helpers for the execution service."""

from __future__ import annotations

from typing import Any

from db.base import utc_now
from db.enums import WorkerNodeStatus, WorkerType, coerce_worker_type
from orchestrator.state import OrchestratorState
from repositories import TaskRepository, WorkerNodeRepository, session_scope
from workers.base import normalize_worker_profile_name
from workers.failure_taxonomy import classify_failure_kind

LEGACY_PROFILE_COMPATIBILITY_NAMES = (
    "antigravity-native-discovery",
    "antigravity-native-executor",
    "antigravity-native-executor-read-only",
    "antigravity-native-planner",
    "antigravity-native-reviewer",
    "antigravity-tool-loop-executor",
    "antigravity-tool-loop-executor-read-only",
    "codex-native-executor",
    "codex-native-executor-read-only",
    "codex-tool-loop-executor",
    "codex-tool-loop-executor-read-only",
    "openrouter-tool-loop-legacy",
)


class WorkerProfileConfigurationError(ValueError):
    """Raised when worker profile routing configuration is malformed."""


def _profile_worker_type(profile_name: str, profile: Any) -> WorkerType:
    if isinstance(profile, dict):
        raw_worker_type = profile.get("worker_type")
    else:
        raw_worker_type = getattr(profile, "worker_type", None)
    if not raw_worker_type:
        raise WorkerProfileConfigurationError(
            f"Invalid worker profile configuration for '{profile_name}': missing worker_type."
        )
    try:
        return coerce_worker_type(raw_worker_type)
    except (TypeError, ValueError) as exc:
        raise WorkerProfileConfigurationError(
            f"Invalid worker profile configuration for '{profile_name}': {exc}"
        ) from exc


def _profile_capability_tags(profile: Any) -> list[Any]:
    if isinstance(profile, dict):
        raw_tags = profile.get("capability_tags", [])
    else:
        raw_tags = getattr(profile, "capability_tags", [])
    return raw_tags if isinstance(raw_tags, list) else []


def _supported_profile_names(self: Any, profiles: dict[str, Any]) -> list[str]:
    if profiles:
        names = {
            normalized
            for profile_name in profiles
            if (normalized := normalize_worker_profile_name(profile_name)) is not None
        }
        return sorted(names)
    if not getattr(self, "enable_worker_profiles", False):
        return list(LEGACY_PROFILE_COMPATIBILITY_NAMES)
    return []


def _supported_worker_types(self: Any) -> set[WorkerType]:
    supported = {WorkerType.CODEX}
    if getattr(self, "gemini_worker", None) is not None:
        supported.add(WorkerType.ANTIGRAVITY)
    if getattr(self, "openrouter_worker", None) is not None:
        supported.add(WorkerType.OPENROUTER)

    for profile_name, profile in getattr(self, "worker_profiles", {}).items():
        supported.add(_profile_worker_type(profile_name, profile))
    return supported


def _worker_node_registration_payload(
    self: Any,
    *,
    capacity: int,
) -> dict[str, Any]:
    """Build persisted worker-node capabilities from the configured service."""
    worker_types = sorted(worker_type.value for worker_type in _supported_worker_types(self))
    primary_worker_type = WorkerType.CODEX.value if "codex" in worker_types else worker_types[0]
    profiles = getattr(self, "worker_profiles", {})
    supported_profiles = _supported_profile_names(self, profiles)
    capability_tags = sorted(
        {
            tag
            for profile in profiles.values()
            for tag in _profile_capability_tags(profile)
            if isinstance(tag, str)
        }
    )
    capabilities = {
        "worker_types": worker_types,
        "lanes": ["primary", "scout"],
        "capability_tags": capability_tags,
    }
    return {
        "worker_type": primary_worker_type,
        "capacity": max(1, capacity),
        "supported_profiles": supported_profiles,
        "capabilities": capabilities,
    }


def register_worker_node(
    self: Any,
    *,
    worker_id: str,
    capacity: int = 1,
    process_identity: str | None = None,
) -> WorkerNodeStatus:
    """Register a queue worker process and return its effective state."""
    now = utc_now()
    payload = _worker_node_registration_payload(self, capacity=capacity)
    with session_scope(self.session_factory) as session:
        node = WorkerNodeRepository(session).register_worker(
            worker_id=worker_id,
            now=now,
            process_identity=process_identity,
            **payload,
        )
        return node.status


def ensure_worker_node(self: Any, *, worker_id: str) -> WorkerNodeStatus:
    """Create a default worker node for compatibility with direct claim callers."""
    now = utc_now()
    payload = _worker_node_registration_payload(self, capacity=1)
    with session_scope(self.session_factory) as session:
        node = WorkerNodeRepository(session).ensure_worker(
            worker_id=worker_id,
            now=now,
            **payload,
        )
        return node.status


def heartbeat_worker_node(self: Any, *, worker_id: str) -> WorkerNodeStatus | None:
    """Record a worker-node heartbeat and return the current status."""
    with session_scope(self.session_factory) as session:
        return WorkerNodeRepository(session).heartbeat(worker_id=worker_id, now=utc_now())


def sweep_worker_nodes(self: Any, *, stale_seconds: int) -> dict[str, int]:
    """Sweep stale worker heartbeats and expired task leases."""
    now = utc_now()
    with session_scope(self.session_factory) as session:
        reclaimed_leases = TaskRepository(session).reclaim_expired_leases(now=now)
        stale_workers = WorkerNodeRepository(session).sweep_stale_workers(
            now=now,
            threshold_seconds=stale_seconds,
        )
        return {
            "reclaimed_leases": reclaimed_leases,
            "stale_workers": stale_workers,
        }


def record_worker_node_success(self: Any, *, worker_id: str) -> None:
    """Reset worker failure accounting after a successful queued run."""
    with session_scope(self.session_factory) as session:
        WorkerNodeRepository(session).record_success(worker_id=worker_id)


def record_worker_node_failure(self: Any, *, worker_id: str, failure_kind: str | None) -> None:
    """Record a typed queued-run failure against the worker node."""
    with session_scope(self.session_factory) as session:
        WorkerNodeRepository(session).record_failure(
            worker_id=worker_id,
            failure_kind=failure_kind,
        )


def worker_node_failure_kind_from_state(state: OrchestratorState) -> str | None:
    """Resolve the typed failure kind used for worker-node health accounting."""
    if state.result is None:
        return "unknown"
    failure_kind = getattr(state.result, "failure_kind", None)
    if failure_kind and failure_kind != "unknown":
        return failure_kind
    return classify_failure_kind(
        status=state.result.status,
        summary=state.result.summary,
        commands_run=state.result.commands_run,
    )


def worker_node_failure_kind_from_exception(exc: Exception) -> str | None:
    """Classify an exception raised by queued task execution."""
    return classify_failure_kind(
        status="error",
        summary=f"{type(exc).__name__}: {exc}",
    )
