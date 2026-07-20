"""Concurrency coverage for immutable runtime cutover evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

from db.base import Base
from db.models import RuntimeCutover
from repositories import (
    RuntimeCutoverRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)


def _initialize_cutover(session_factory, configured_at: datetime, barrier: Barrier):
    barrier.wait()
    try:
        with session_scope(session_factory) as session:
            return RuntimeCutoverRepository(session).initialize_temporal_only(configured_at)
    except Exception as exc:  # Tests assert the public outcome below.
        return exc


def test_concurrent_cutover_initialization_is_idempotent_and_conflict_safe(tmp_path) -> None:
    """Same configuration shares one record; conflicting configuration fails explicitly."""
    same_at = datetime(2026, 7, 19, 9, tzinfo=UTC)
    engine = create_engine_from_url(
        f"sqlite+pysqlite:///{tmp_path / 'same-cutover.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    same_barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: _initialize_cutover(session_factory, same_at, same_barrier),
                range(2),
            )
        )

    assert results == [same_at, same_at]
    with session_scope(session_factory) as session:
        assert session.query(RuntimeCutover).count() == 1

    conflicting_at = datetime(2026, 7, 20, 9, tzinfo=UTC)
    conflict_engine = create_engine_from_url(
        f"sqlite+pysqlite:///{tmp_path / 'conflicting-cutover.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(conflict_engine)
    conflict_factory = create_session_factory(conflict_engine)
    conflict_barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda configured_at: _initialize_cutover(
                    conflict_factory, configured_at, conflict_barrier
                ),
                [same_at, conflicting_at],
            )
        )

    persisted_at = next(result for result in results if isinstance(result, datetime))
    conflicts = [result for result in results if isinstance(result, RuntimeError)]
    assert len(conflicts) == 1
    assert "conflicts with the persisted" in str(conflicts[0])
    with session_scope(conflict_factory) as session:
        persisted = session.query(RuntimeCutover).one()
        assert persisted.cutover_at.replace(tzinfo=UTC) == persisted_at
