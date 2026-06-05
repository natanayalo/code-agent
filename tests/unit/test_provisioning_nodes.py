"""Unit tests for workspace provisioning and environment initialization nodes."""

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


def test_provision_workspace_node_creates_new_workspace() -> None:
    """Verify that the node creates a workspace if one doesn't exist."""
    manager = MagicMock()
    handle = MagicMock()
    handle.workspace_id = "ws-123"
    manager.create_workspace.return_value = handle

    node = build_provision_workspace_node(manager)
    state = OrchestratorState(
        task={
            "task_id": "t1",
            "repo_url": "https://github.com/org/repo",
            "task_text": "fix bug",
        },
        dispatch={},
    )

    result = node(state)

    assert result["dispatch"]["workspace_id"] == "ws-123"
    assert result["current_step"] == "provision_workspace"
    manager.create_workspace.assert_called_once()


def test_provision_workspace_node_reuses_existing_workspace() -> None:
    """Verify that the node reuses an existing workspace_id."""
    manager = MagicMock()
    node = build_provision_workspace_node(manager)
    state = OrchestratorState(
        task={
            "task_id": "t1",
            "repo_url": "https://github.com/org/repo",
            "task_text": "fix bug",
        },
        dispatch={"workspace_id": "existing-ws"},
    )

    result = node(state)

    assert result["current_step"] == "provision_workspace"
    assert "dispatch" not in result  # No update needed
    manager.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_init_environment_node_detects_poetry(tmp_path: Path) -> None:
    """Verify detection of poetry.lock."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "poetry.lock").write_text("lock")
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    shell_worker.run.return_value = WorkerResult(status="success", summary="ok")

    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
        dispatch={"workspace_id": "ws-1"},
    )

    result = await node(state)

    assert result["current_step"] == "init_environment"
    assert shell_worker.run.call_count >= 1
    init_call_args = shell_worker.run.call_args_list[0][0][0]
    assert "poetry install" in init_call_args.task_text


@pytest.mark.asyncio
async def test_init_environment_skips_gitignore_hardening_for_read_only_route(
    tmp_path: Path,
) -> None:
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "poetry.lock").write_text("lock")
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    shell_worker.run.return_value = WorkerResult(status="success", summary="ok")

    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
            "task_spec": {
                "goal": "print PWD and HOME",
                "non_goals": ["Do not modify any files."],
            },
            "route": {"chosen_profile": "codex-native-executor-read-only"},
            "dispatch": {"workspace_id": "ws-1"},
        }
    )

    result = await node(state)

    assert shell_worker.run.call_count == 1
    assert "poetry install" in shell_worker.run.call_args_list[0][0][0].task_text
    payload = result["timeline_events"][0].payload
    assert payload["hardened_ignores"] == []
    assert payload["hardening_skipped_reason"] == "read_only_or_no_modification_task"


@pytest.mark.asyncio
async def test_init_environment_skips_gitignore_hardening_for_no_mutation_task_spec(
    tmp_path: Path,
) -> None:
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "poetry.lock").write_text("lock")
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    shell_worker.run.return_value = WorkerResult(status="success", summary="ok")

    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_id": "t1",
                "repo_url": "r1",
                "task_text": "Smoke test: print PWD and HOME only, then exit.",
            },
            "task_spec": {
                "goal": "Smoke test: print PWD and HOME only, then exit.",
                "allowed_actions": ["read_repo_files", "run_non_destructive_checks"],
            },
            "route": {"chosen_profile": "codex-native-executor"},
            "dispatch": {"workspace_id": "ws-1"},
        }
    )

    result = await node(state)

    assert shell_worker.run.call_count == 1
    payload = result["timeline_events"][0].payload
    assert payload["hardened_ignores"] == []
    assert payload["hardening_skipped_reason"] == "read_only_or_no_modification_task"


@pytest.mark.asyncio
async def test_init_environment_runs_gitignore_hardening_for_mutation_capable_task(
    tmp_path: Path,
) -> None:
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "poetry.lock").write_text("lock")
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    shell_worker.run.side_effect = [
        WorkerResult(status="success", summary="ok"),
        WorkerResult(status="success", summary="hardened", stdout="hardened: .cache"),
    ]

    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
            "task_spec": {"goal": "edit files"},
            "route": {"chosen_profile": "codex-native-executor"},
            "dispatch": {"workspace_id": "ws-1"},
        }
    )

    result = await node(state)

    assert shell_worker.run.call_count == 2
    assert "git check-ignore" in shell_worker.run.call_args_list[1][0][0].task_text
    payload = result["timeline_events"][0].payload
    assert payload["hardened_ignores"] == [".cache"]
    assert payload["hardening_skipped_reason"] is None


@pytest.mark.asyncio
async def test_init_environment_node_detects_uv(tmp_path: Path) -> None:
    """Verify detection of uv.lock."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "uv.lock").write_text("lock")
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
    assert "uv sync" in init_call_args.task_text


@pytest.mark.asyncio
async def test_init_environment_node_detects_pip(tmp_path: Path) -> None:
    """Verify detection of requirements.txt."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "requirements.txt").write_text("reqs")
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    shell_worker.run.return_value = WorkerResult(status="success", summary="ok")

    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={
            "task_id": "t1",
            "repo_url": "r1",
            "task_text": "setup",
            "constraints": {"allow_non_reproducible_install": True},
        },
        dispatch={"workspace_id": "ws-1"},
    )

    await node(state)
    assert shell_worker.run.call_count >= 1
    init_call_args = shell_worker.run.call_args_list[0][0][0]
    assert "pip install" in init_call_args.task_text


@pytest.mark.asyncio
async def test_init_environment_node_detects_yarn(tmp_path: Path) -> None:
    """Verify detection of yarn.lock."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "yarn.lock").write_text("lock")
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
    assert "yarn install" in init_call_args.task_text


@pytest.mark.asyncio
async def test_init_environment_node_fails_on_missing_lockfile(tmp_path: Path) -> None:
    """Verify hard-fail policy for missing lockfiles."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "pyproject.toml").write_text("toml")
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
        dispatch={"workspace_id": "ws-1"},
    )

    result = await node(state)

    assert result["result"].status == "error"
    assert "Missing lockfile" in result["result"].summary
    shell_worker.run.assert_not_called()


@pytest.mark.asyncio
async def test_init_environment_node_fails_on_worker_error(tmp_path: Path) -> None:
    """Verify failure path when shell_worker fails."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "poetry.lock").write_text("lock")
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    shell_worker.run.return_value = WorkerResult(status="failure", summary="install failed")

    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
        dispatch={"workspace_id": "ws-1"},
    )

    result = await node(state)

    assert result["result"]["status"] == "failure"
    assert result["result"]["summary"] == "install failed"


@pytest.mark.asyncio
async def test_init_environment_node_allows_non_reproducible(tmp_path: Path) -> None:
    """Verify fallback to poetry/npm when allow_non_reproducible_install is True."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "pyproject.toml").write_text("toml")
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    shell_worker.run.return_value = WorkerResult(status="success", summary="ok")

    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={
            "task_id": "t1",
            "repo_url": "r1",
            "task_text": "setup",
            "constraints": {"allow_non_reproducible_install": True},
        },
        dispatch={"workspace_id": "ws-1"},
    )

    await node(state)
    assert shell_worker.run.call_count >= 1
    init_call_args = shell_worker.run.call_args_list[0][0][0]
    assert "poetry install" in init_call_args.task_text


@pytest.mark.asyncio
async def test_init_environment_node_fails_on_requirements_without_override(tmp_path: Path) -> None:
    """Verify that requirements.txt fails if allow_non_reproducible_install is False."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "requirements.txt").write_text("reqs")
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
        dispatch={"workspace_id": "ws-1"},
    )

    result = await node(state)

    assert result["result"].status == "error"
    assert "Missing lockfile" in result["result"].summary
    shell_worker.run.assert_not_called()


@pytest.mark.asyncio
async def test_init_environment_node_missing_shell_worker() -> None:
    """Verify that the node skips if shell_worker is not provided."""
    manager = MagicMock()
    node = build_init_environment_node(manager, None)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
        dispatch={"workspace_id": "ws-1"},
    )
    result = await node(state)
    assert result["current_step"] == "init_environment"
    assert result.get("result") is None


@pytest.mark.asyncio
async def test_init_environment_node_missing_workspace_id() -> None:
    """Verify that the node raises error if workspace_id is missing."""
    manager = MagicMock()
    shell_worker = AsyncMock()
    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
        dispatch={},
    )
    with pytest.raises(RuntimeError, match="called before provision_workspace"):
        await node(state)


@pytest.mark.asyncio
async def test_init_environment_node_detects_pnpm(tmp_path: Path) -> None:
    """Verify detection of pnpm-lock.yaml."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "pnpm-lock.yaml").write_text("lock")
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
    assert "pnpm install" in init_call_args.task_text


@pytest.mark.asyncio
async def test_init_environment_node_detects_npm(tmp_path: Path) -> None:
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
async def test_init_environment_node_detects_cargo(tmp_path: Path) -> None:
    """Verify detection of Cargo.toml."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "Cargo.toml").write_text("toml")
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
    assert "cargo fetch" in init_call_args.task_text


@pytest.mark.asyncio
async def test_init_environment_node_detects_go(tmp_path: Path) -> None:
    """Verify detection of go.mod."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "go.mod").write_text("mod")
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
    assert "go mod download" in init_call_args.task_text


@pytest.mark.asyncio
async def test_init_environment_node_detects_makefile(tmp_path: Path) -> None:
    """Verify detection of Makefile."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "Makefile").write_text("all:")
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
    assert "make setup" in init_call_args.task_text


@pytest.mark.asyncio
async def test_init_environment_node_no_setup_file(tmp_path: Path) -> None:
    """Verify behavior when no setup file is found."""
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
async def test_init_environment_node_detects_npm_no_lock(tmp_path: Path) -> None:
    """Verify detection of package.json without lockfile when override allowed."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "package.json").write_text("{}")
    manager.get_workspace.return_value = handle
    shell_worker = AsyncMock()
    shell_worker.run.return_value = WorkerResult(status="success", summary="ok")
    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={
            "task_id": "t1",
            "repo_url": "r1",
            "task_text": "setup",
            "constraints": {"allow_non_reproducible_install": True},
        },
        dispatch={"workspace_id": "ws-1"},
    )
    await node(state)
    assert shell_worker.run.call_count >= 1
    init_call_args = shell_worker.run.call_args_list[0][0][0]
    assert "npm install" in init_call_args.task_text


@pytest.mark.asyncio
async def test_init_environment_node_hardens_gitignore(tmp_path: Path) -> None:
    """Verify that .gitignore is hardened when noise patterns are missing."""
    manager = MagicMock()
    handle = MagicMock()
    handle.repo_path = tmp_path
    (tmp_path / "poetry.lock").write_text("lock")
    manager.get_workspace.return_value = handle

    shell_worker = AsyncMock()
    # Mock return values for:
    # 1. poetry install
    # 2. git hardening script
    shell_worker.run.side_effect = [
        WorkerResult(status="success", summary="install ok"),
        WorkerResult(status="success", stdout="hardened:.cache __pycache__"),
    ]

    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={"task_id": "t1", "repo_url": "r1", "task_text": "setup"},
        dispatch={"workspace_id": "ws-1"},
    )

    result = await node(state)

    # Verify that the ENVIRONMENT_INITIALIZED event contains the hardening info
    init_events = [
        e for e in result.get("timeline_events", []) if e.event_type == "environment_initialized"
    ]
    assert any("Proactively hardened .gitignore" in e.message for e in init_events)
    # The consolidated payload in the final event
    final_event = init_events[-1]
    assert final_event.payload["hardened_ignores"] == [".cache", "__pycache__"]


@pytest.mark.asyncio
async def test_init_environment_node_fails_on_npm_no_lock_without_override(tmp_path: Path) -> None:
    """Verify failure of package.json without lockfile when override not allowed."""
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
    assert result["result"].status == "error"
    assert "Missing lockfile" in result["result"].summary
    shell_worker.run.assert_not_called()
