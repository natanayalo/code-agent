"""Hardening tests to reach >95% coverage on provisioning and runtime logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.nodes.provisioning import (
    build_init_environment_node,
    build_provision_workspace_node,
)
from orchestrator.state import OrchestratorState
from workers import WorkerResult
from workers.cli_runtime import (
    CliRuntimeStep,
    _looks_read_only_command,
)


class _ScriptedAdapter:
    def __init__(self, steps: list[CliRuntimeStep]) -> None:
        self._steps = list(steps)

    def next_step(self, *args, **kwargs) -> CliRuntimeStep:
        if not self._steps:
            return CliRuntimeStep(kind="final", final_output="done")
        return self._steps.pop(0)


def test_looks_read_only_command_edge_cases() -> None:
    """Cover empty and whitespace command classification."""
    assert _looks_read_only_command("") is True
    assert _looks_read_only_command("   ") is True
    assert _looks_read_only_command("\n\t") is True

    assert _looks_read_only_command("\n\t") is True


@pytest.mark.asyncio
async def test_init_environment_node_no_shell_worker(tmp_path: Path) -> None:
    """Verify that the node skips setup if no shell_worker is provided."""
    manager = MagicMock()
    node = build_init_environment_node(manager, shell_worker=None)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
        dispatch={"workspace_id": "ws-1"},
    )

    result = await node(state)
    assert result["current_step"] == "init_environment"
    assert result.get("result") is None
    """Verify that the node does nothing if no markers are found."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
        dispatch={"workspace_id": "ws-1"},
    )

    result = await node(state)

    assert result["current_step"] == "init_environment"
    assert result.get("result") is None
    shell_worker.run.assert_not_called()


@pytest.mark.asyncio
async def test_init_environment_node_missing_workspace_id() -> None:
    """Verify that init_environment raises RuntimeError if workspace_id is missing."""
    manager = MagicMock()
    shell_worker = AsyncMock()
    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
        dispatch={},  # Missing workspace_id
    )

    with pytest.raises(RuntimeError, match="provision_workspace"):
        await node(state)


@pytest.mark.asyncio
async def test_init_environment_node_detects_npm_ci(tmp_path: Path) -> None:
    """Verify detection of package-lock.json."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "package-lock.json").write_text("lock")
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    shell_worker.run.return_value = WorkerResult(status="success", summary="ok")

    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
        dispatch={"workspace_id": "ws-1"},
    )

    await node(state)
    assert shell_worker.run.call_count >= 1
    init_call_args = shell_worker.run.call_args_list[0][0][0]
    assert "npm ci" in init_call_args.task_text


@pytest.mark.asyncio
async def test_init_environment_node_fails_on_missing_node_lockfile(tmp_path: Path) -> None:
    """Verify error message for missing Node lockfile."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "package.json").write_text("{}")
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
        dispatch={"workspace_id": "ws-1"},
    )

    result = await node(state)
    assert result["result"]["status"] == "error"
    assert "Missing lockfile" in result["result"]["summary"]


def test_provision_workspace_node_logic(tmp_path: Path) -> None:
    """Verify workspace creation and logging branches."""
    manager = MagicMock()
    handle = MagicMock()
    handle.workspace_id = "new-ws"
    manager.create_workspace.return_value = handle

    node = build_provision_workspace_node(manager)

    # Path: session exists (with all required fields)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "go"},
        dispatch={},
        session={
            "session_id": "s1",
            "user_id": "u1",
            "channel": "telegram",
            "external_thread_id": "123",
            "active_task_id": "t1",
            "status": "active",
        },
    )
    result = node(state)
    assert result["dispatch"]["workspace_id"] == "new-ws"
