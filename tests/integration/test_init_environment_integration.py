"""Integration tests for environment initialization with real Docker containers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.nodes.provisioning import build_init_environment_node
from orchestrator.state import OrchestratorState
from sandbox import (
    DockerSandboxContainerManager,
    WorkspaceManager,
    WorkspaceRequest,
)
from workers import ShellWorker


def _docker_and_image_available(image: str) -> bool:
    try:
        docker_info = subprocess.run(
            ["docker", "info"],
            check=False,
            capture_output=True,
            text=True,
        )
        if docker_info.returncode != 0:
            return False

        image_inspect = subprocess.run(
            ["docker", "image", "inspect", image],
            check=False,
            capture_output=True,
            text=True,
        )
        return image_inspect.returncode == 0
    except FileNotFoundError:
        return False


def _run_git(command: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _create_poetry_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    _run_git(["git", "init", "--initial-branch=main"], cwd=repo_path)

    pyproject = """[tool.poetry]
name = "test-project"
version = "0.1.0"
description = ""
authors = ["Test <test@example.com>"]
package-mode = false

[tool.poetry.dependencies]
python = "^3.12"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
"""
    (repo_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    # We don't create a real lockfile, we'll use allow_non_reproducible_install=True
    # to trigger 'poetry install' which will generate one.

    _run_git(["git", "add", "."], cwd=repo_path)
    _run_git(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        ],
        cwd=repo_path,
    )


@pytest.mark.asyncio
async def test_init_environment_integration_poetry(tmp_path: Path):
    """Verify that init_environment correctly configures poetry and creates a .venv."""
    image = os.environ.get("CODE_AGENT_SANDBOX_IMAGE", "code-agent-worker:latest")
    if not _docker_and_image_available(image):
        pytest.skip(f"Docker or image {image!r} unavailable")

    # 1. Setup real infrastructure
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir()
    manager = WorkspaceManager(ws_root)

    container_manager = DockerSandboxContainerManager(default_image=image)
    shell_worker = ShellWorker(
        workspace_root=ws_root,
        container_manager=container_manager,
    )

    # 2. Create a dummy repo with pyproject.toml
    source_repo = tmp_path / "source-repo"
    _create_poetry_repo(source_repo)

    # 3. Create workspace
    workspace = manager.create_workspace(
        WorkspaceRequest(
            task_id="test-init-task",
            repo_url=f"file://{source_repo.resolve()}",
        )
    )

    # 4. Run the node
    node = build_init_environment_node(manager, shell_worker)
    state = OrchestratorState(
        task={
            "task_id": "test-init-task",
            "repo_url": f"file://{source_repo.resolve()}",
            "task_text": "Setup the environment",
            "constraints": {"allow_non_reproducible_install": True},
        },
        dispatch={"workspace_id": workspace.workspace_id},
    )

    result = await node(state)

    # 5. Verify results
    if not result or result.get("result", {}).get("status") != "success":
        res = result.get("result", {}) if result else {}
        summary = res.get("summary", "unknown failure")
        stdout = res.get("stdout", "n/a")
        stderr = res.get("stderr", "n/a")
        pytest.fail(f"init_environment failed: {summary}\nSTDOUT: {stdout}\nSTDERR: {stderr}")

    # Check that poetry.toml was created (persistent config)
    poetry_toml_path = workspace.workspace_path / "poetry.toml"
    assert poetry_toml_path.exists(), "poetry.toml should be created in the workspace"

    content = poetry_toml_path.read_text(encoding="utf-8")
    assert "in-project = true" in content, "poetry.toml should have in-project = true"

    # Check that .venv was created
    venv_path = workspace.workspace_path / ".venv"
    assert venv_path.exists(), ".venv should be created in the workspace"

    bin_dir = venv_path / "bin"
    if not bin_dir.exists():
        # Maybe it's 'Scripts' (Windows, though tests run on Mac/Linux)
        bin_dir = venv_path / "Scripts"

    assert bin_dir.exists(), f"Venv bin directory missing. Contents: {list(venv_path.glob('*'))}"

    python_bin = bin_dir / "python"
    if not os.path.lexists(str(python_bin)):
        python_bin = bin_dir / "python3"

    assert os.path.lexists(
        str(python_bin)
    ), f"Venv python binary missing. Bin contents: {list(bin_dir.glob('*'))}"
