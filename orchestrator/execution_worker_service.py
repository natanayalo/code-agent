"""Worker-node registry helpers for the execution service."""

from __future__ import annotations

from typing import Any

from db.base import utc_now
from db.enums import WorkerNodeStatus, WorkerType, coerce_worker_type
from orchestrator.state import OrchestratorState
from repositories import TaskRepository, WorkerNodeRepository, session_scope
from workers.failure_taxonomy import classify_failure_kind


def _supported_worker_types(self: Any) -> set[WorkerType]:
    supported = {WorkerType.CODEX}
    if getattr(self, "gemini_worker", None) is not None:
        supported.add(WorkerType.ANTIGRAVITY)
    if getattr(self, "openrouter_worker", None) is not None:
        supported.add(WorkerType.OPENROUTER)

    for profile in getattr(self, "worker_profiles", {}).values():
        try:
            supported.add(coerce_worker_type(profile.worker_type))
        except ValueError:
            continue
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
    supported_profiles = sorted(profiles)
    capability_tags = sorted(
        {
            tag
            for profile in profiles.values()
            for tag in getattr(profile, "capability_tags", [])
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
