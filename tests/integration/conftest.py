"""Shared integration fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from db.base import Base
from orchestrator.execution import TaskExecutionService
from repositories import create_engine_from_url, create_session_factory
from tests.integration.task_endpoints_support import DEFAULT_SHARED_SECRET, _default_worker


@pytest.fixture(autouse=True)
def _use_legacy_runtime_for_queue_oriented_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep pre-cutover queue fixtures explicit about their fallback runtime.

    Temporal-focused scenarios opt in within the individual test before creating
    a task, matching the production selector contract.
    """
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "legacy")


@pytest.fixture
def session_factory():
    """Create a SQLite-backed session factory for repository integration tests."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def client(session_factory, tmp_path) -> Iterator[TestClient]:
    """Provide a test client with the execution-path task service configured."""
    worker = _default_worker()
    checkpoint_file = tmp_path / "test_checkpoints.sqlite"
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=worker,
            checkpoint_path=str(checkpoint_file),
        ),
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )
    app.state.test_worker = worker
    with TestClient(app) as test_client:
        test_client.headers["X-Webhook-Token"] = DEFAULT_SHARED_SECRET
        yield test_client
