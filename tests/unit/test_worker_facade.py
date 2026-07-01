"""Unit tests for the WorkerFacade class."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from workers.base import WorkerRequest, WorkerResult
from workers.facade import WorkerFacade


def test_worker_facade_routing() -> None:
    """Facade should correctly route to registered workers."""
    antigravity_worker = Mock()
    codex_worker = Mock()
    openrouter_worker = Mock()
    shell_worker = Mock()

    facade = WorkerFacade(
        antigravity_worker=antigravity_worker,
        codex_worker=codex_worker,
        openrouter_worker=openrouter_worker,
        shell_worker=shell_worker,
    )

    assert facade.get_worker("antigravity") is antigravity_worker
    assert facade.get_worker("codex") is codex_worker
    assert facade.get_worker("openrouter") is openrouter_worker
    assert facade.get_shell_worker() is shell_worker

    available = facade.available_workers()
    assert "antigravity" in available
    assert "codex" in available
    assert "openrouter" in available
    assert "shell" in available


@pytest.mark.anyio
async def test_worker_facade_run_missing_type() -> None:
    """Facade run should fail if no worker type is specified or discoverable."""
    facade = WorkerFacade()
    req = WorkerRequest(task_text="test missing")

    result = await facade.run(req)
    assert result.status == "error"
    assert "no worker_type specified" in result.summary


@pytest.mark.anyio
async def test_worker_facade_run_conflict() -> None:
    """Facade run should fail if explicit type conflicts with manifest."""
    facade = WorkerFacade()
    req = WorkerRequest(
        worker_type="codex",
        task_text="test conflict",
        runtime_manifest={"worker": {"worker_type": "antigravity"}},
    )

    result = await facade.run(req)
    assert result.status == "error"
    assert "contract error" in result.summary


@pytest.mark.anyio
async def test_worker_facade_run_success() -> None:
    """Facade run should properly delegate to a concrete worker."""
    mock_worker = AsyncMock()
    mock_worker.run.return_value = WorkerResult(
        status="success",
        summary="done",
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
    )

    facade = WorkerFacade(antigravity_worker=mock_worker)
    req = WorkerRequest(worker_type="antigravity", task_text="test route")

    result = await facade.run(req)
    assert result.status == "success"
    assert result.summary == "done"
    mock_worker.run.assert_called_once()
