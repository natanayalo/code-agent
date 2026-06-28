"""Worker-node registry repositories."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from db.enums import WorkerNodeStatus, WorkerType, coerce_worker_type
from db.models import WorkerNode

QUARANTINE_FAILURE_KINDS = frozenset({"provider_auth", "provider_error", "sandbox_infra"})
DEFAULT_FAILURE_QUARANTINE_THRESHOLD = 3


class WorkerNodeRepository:
    """Persist worker-process health, capacity, and quarantine state."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_worker_id(self, worker_id: str) -> WorkerNode | None:
        statement = select(WorkerNode).where(WorkerNode.worker_id == worker_id)
        return self.session.scalar(statement)

    def register_worker(
        self,
        *,
        worker_id: str,
        worker_type: str | WorkerType,
        now: datetime,
        capacity: int = 1,
        process_identity: str | None = None,
        supported_profiles: list[str] | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> WorkerNode:
        """Create or refresh a worker node, preserving explicit quarantines."""
        normalized_worker_type = coerce_worker_type(worker_type)
        bounded_capacity = max(1, capacity)
        node = self.get_by_worker_id(worker_id)
        if node is None:
            node = WorkerNode(
                worker_id=worker_id,
                worker_type=normalized_worker_type,
                status=WorkerNodeStatus.ACTIVE,
                process_identity=process_identity,
                supported_profiles=supported_profiles or [],
                capabilities=capabilities or {},
                last_heartbeat_at=now,
                capacity=bounded_capacity,
                current_load=0,
                consecutive_failures=0,
            )
            self.session.add(node)
            self.session.flush()
            return node

        was_quarantined = node.status == WorkerNodeStatus.QUARANTINED
        node.worker_type = normalized_worker_type
        node.process_identity = process_identity
        node.supported_profiles = supported_profiles or []
        node.capabilities = capabilities or {}
        node.last_heartbeat_at = now
        node.capacity = bounded_capacity
        node.current_load = 0
        if not was_quarantined:
            node.status = WorkerNodeStatus.ACTIVE
            node.quarantine_reason = None
            node.consecutive_failures = 0
        self.session.flush()
        return node

    def ensure_worker(
        self,
        *,
        worker_id: str,
        worker_type: str | WorkerType,
        now: datetime,
        capacity: int = 1,
        process_identity: str | None = None,
        supported_profiles: list[str] | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> WorkerNode:
        """Create a missing worker without resetting an existing node's load."""
        node = self.get_by_worker_id(worker_id)
        if node is not None:
            return node
        return self.register_worker(
            worker_id=worker_id,
            worker_type=worker_type,
            now=now,
            capacity=capacity,
            process_identity=process_identity,
            supported_profiles=supported_profiles,
            capabilities=capabilities,
        )

    def heartbeat(self, *, worker_id: str, now: datetime) -> WorkerNodeStatus | None:
        """Record a worker heartbeat and return its current status."""
        node = self.get_by_worker_id(worker_id)
        if node is None:
            return None
        node.last_heartbeat_at = now
        self.session.flush()
        return node.status

    def reserve_load(self, *, worker_id: str) -> bool:
        """Atomically reserve one unit of capacity for an active worker."""
        updated = self.session.execute(
            update(WorkerNode)
            .where(
                WorkerNode.worker_id == worker_id,
                WorkerNode.status == WorkerNodeStatus.ACTIVE,
                WorkerNode.current_load < WorkerNode.capacity,
            )
            .values(current_load=WorkerNode.current_load + 1)
            .execution_options(synchronize_session="fetch")
        )
        updated_rows = int(getattr(updated, "rowcount", 0) or 0)
        if updated_rows <= 0:
            return False
        self.session.flush()
        return True

    def release_load(self, *, worker_id: str, count: int = 1) -> bool:
        """Atomically release worker capacity without allowing negative load."""
        bounded_count = max(1, count)
        updated = self.session.execute(
            update(WorkerNode)
            .where(WorkerNode.worker_id == worker_id)
            .values(
                current_load=case(
                    (
                        WorkerNode.current_load >= bounded_count,
                        WorkerNode.current_load - bounded_count,
                    ),
                    else_=0,
                )
            )
            .execution_options(synchronize_session="fetch")
        )
        updated_rows = int(getattr(updated, "rowcount", 0) or 0)
        if updated_rows <= 0:
            return False
        self.session.flush()
        return True

    def set_load(self, *, worker_id: str, current_load: int) -> bool:
        """Set load from authoritative lease state after reconciliation."""
        bounded_load = max(0, current_load)
        updated = self.session.execute(
            update(WorkerNode)
            .where(WorkerNode.worker_id == worker_id)
            .values(
                current_load=case(
                    (WorkerNode.capacity >= bounded_load, bounded_load),
                    else_=WorkerNode.capacity,
                )
            )
            .execution_options(synchronize_session="fetch")
        )
        updated_rows = int(getattr(updated, "rowcount", 0) or 0)
        if updated_rows <= 0:
            return False
        self.session.flush()
        return True

    def record_failure(
        self,
        *,
        worker_id: str,
        failure_kind: str | None,
        threshold: int = DEFAULT_FAILURE_QUARANTINE_THRESHOLD,
    ) -> WorkerNode | None:
        """Track provider/infra failures and quarantine repeatedly failing workers."""
        node = self.get_by_worker_id(worker_id)
        if node is None:
            return None
        if failure_kind not in QUARANTINE_FAILURE_KINDS:
            node.consecutive_failures = 0
            self.session.flush()
            return node

        node.consecutive_failures += 1
        if node.consecutive_failures >= max(1, threshold):
            node.status = WorkerNodeStatus.QUARANTINED
            node.quarantine_reason = (
                f"Consecutive {failure_kind} failures reached {node.consecutive_failures}."
            )
        self.session.flush()
        return node

    def record_success(self, *, worker_id: str) -> WorkerNode | None:
        """Reset consecutive failure accounting after a successful run."""
        node = self.get_by_worker_id(worker_id)
        if node is None:
            return None
        node.consecutive_failures = 0
        self.session.flush()
        return node

    def sweep_stale_workers(self, *, now: datetime, threshold_seconds: int) -> int:
        """Mark non-quarantined workers offline after missed heartbeats."""
        cutoff = now - timedelta(seconds=max(1, threshold_seconds))
        updated = self.session.execute(
            update(WorkerNode)
            .where(
                WorkerNode.status.in_([WorkerNodeStatus.ACTIVE, WorkerNodeStatus.DRAINING]),
                WorkerNode.last_heartbeat_at < cutoff,
            )
            .values(status=WorkerNodeStatus.OFFLINE, current_load=0)
            .execution_options(synchronize_session="fetch")
        )
        updated_rows = int(getattr(updated, "rowcount", 0) or 0)
        if updated_rows > 0:
            self.session.flush()
        return updated_rows

    @staticmethod
    def supported_worker_types(node: WorkerNode) -> set[WorkerType]:
        """Return worker types this process can route to."""
        worker_types = []
        capabilities = node.capabilities if isinstance(node.capabilities, dict) else {}
        raw_worker_types = capabilities.get("worker_types")
        if isinstance(raw_worker_types, list):
            worker_types.extend(raw_worker_types)
        worker_types.append(node.worker_type)
        supported: set[WorkerType] = set()
        for value in worker_types:
            try:
                supported.add(coerce_worker_type(cast(object, value)))
            except (ValueError, TypeError):
                continue
        return supported
