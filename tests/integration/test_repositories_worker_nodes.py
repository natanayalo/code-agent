"""Integration tests for worker-node registry behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from db.enums import WorkerNodeStatus
from repositories import WorkerNodeRepository, session_scope


def test_worker_node_registration_resets_load_but_preserves_quarantine(session_factory) -> None:
    """Re-registering a quarantined worker should not silently re-enable it."""
    now = datetime.now(UTC)
    with session_scope(session_factory) as session:
        repo = WorkerNodeRepository(session)
        node = repo.register_worker(
            worker_id="worker-a",
            worker_type="codex",
            now=now,
            capacity=2,
            process_identity="host:1",
            supported_profiles=["codex-native-executor"],
            capabilities={"worker_types": ["codex"], "lanes": ["primary"]},
        )
        assert node.status is WorkerNodeStatus.ACTIVE
        assert repo.reserve_load(worker_id="worker-a") is True
        assert repo.reserve_load(worker_id="worker-a") is True
        assert repo.reserve_load(worker_id="worker-a") is False

        repo.record_failure(worker_id="worker-a", failure_kind="provider_auth", threshold=1)
        assert node.status is WorkerNodeStatus.QUARANTINED
        assert node.quarantine_reason is not None

        refreshed = repo.register_worker(
            worker_id="worker-a",
            worker_type="codex",
            now=now + timedelta(seconds=10),
            capacity=4,
            process_identity="host:2",
            supported_profiles=["codex-native-executor"],
            capabilities={"worker_types": ["codex"], "lanes": ["primary"]},
        )

        assert refreshed.status is WorkerNodeStatus.QUARANTINED
        assert refreshed.current_load == 0
        assert refreshed.capacity == 4
        assert refreshed.process_identity == "host:2"
        assert refreshed.quarantine_reason is not None


def test_worker_node_failure_accounting_only_quarantines_provider_and_infra(
    session_factory,
) -> None:
    """User-code failures should not count toward worker quarantine."""
    now = datetime.now(UTC)
    with session_scope(session_factory) as session:
        repo = WorkerNodeRepository(session)
        node = repo.register_worker(
            worker_id="worker-b",
            worker_type="codex",
            now=now,
            capacity=1,
        )

        repo.record_failure(worker_id="worker-b", failure_kind="test", threshold=1)
        assert node.status is WorkerNodeStatus.ACTIVE
        assert node.consecutive_failures == 0

        repo.record_failure(worker_id="worker-b", failure_kind="sandbox_infra", threshold=2)
        assert node.status is WorkerNodeStatus.ACTIVE
        assert node.consecutive_failures == 1

        repo.record_failure(worker_id="worker-b", failure_kind="provider_error", threshold=2)
        assert node.status is WorkerNodeStatus.QUARANTINED
        assert node.consecutive_failures == 2


def test_worker_node_sweep_marks_stale_active_workers_offline_without_clearing_quarantine(
    session_factory,
) -> None:
    """Heartbeat sweeps should not overwrite explicit quarantines."""
    now = datetime.now(UTC)
    with session_scope(session_factory) as session:
        repo = WorkerNodeRepository(session)
        active = repo.register_worker(
            worker_id="worker-active",
            worker_type="codex",
            now=now - timedelta(minutes=10),
            capacity=1,
        )
        quarantined = repo.register_worker(
            worker_id="worker-quarantined",
            worker_type="codex",
            now=now - timedelta(minutes=10),
            capacity=1,
        )
        repo.record_failure(
            worker_id="worker-quarantined",
            failure_kind="provider_auth",
            threshold=1,
        )

        swept = repo.sweep_stale_workers(now=now, threshold_seconds=60)

        assert swept == 1
        assert active.status is WorkerNodeStatus.OFFLINE
        assert quarantined.status is WorkerNodeStatus.QUARANTINED


def test_worker_node_release_load_never_goes_negative(session_factory) -> None:
    """Load release should be idempotent enough for cancelled/lost lease paths."""
    now = datetime.now(UTC)
    with session_scope(session_factory) as session:
        repo = WorkerNodeRepository(session)
        node = repo.register_worker(
            worker_id="worker-release",
            worker_type="codex",
            now=now,
            capacity=1,
        )

        assert repo.reserve_load(worker_id="worker-release") is True
        assert node.current_load == 1
        assert repo.release_load(worker_id="worker-release") is True
        assert node.current_load == 0
        assert repo.release_load(worker_id="worker-release") is True
        assert node.current_load == 0
