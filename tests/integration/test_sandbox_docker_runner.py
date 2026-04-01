"""Integration tests for the Docker sandbox runner."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from sandbox import DockerSandboxCommand, DockerSandboxRunner, WorkspaceManager, WorkspaceRequest


def _run_git(command: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _create_local_repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "source-repo"
    repo_path.mkdir()
    _run_git(["git", "init", "--initial-branch", "main"], cwd=repo_path)
    (repo_path / "README.md").write_text("sandbox runner\n", encoding="utf-8")
    _run_git(["git", "add", "README.md"], cwd=repo_path)
    _run_git(
        [
            "git",
            "-c",
            "user.name=Codex",
            "-c",
            "user.email=codex@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo_path,
    )
    return repo_path


def _docker_and_image_available(image: str) -> bool:
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


def test_docker_sandbox_runner_executes_command_in_container(tmp_path: Path) -> None:
    """A mounted workspace command should run in Docker and write back into the repo."""
    image = os.environ.get("CODE_AGENT_TEST_DOCKER_IMAGE", "python:3.12-slim")
    if not _docker_and_image_available(image):
        pytest.skip(f"Docker daemon or image {image!r} is unavailable")

    source_repo = _create_local_repo(tmp_path)
    workspace_manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = workspace_manager.create_workspace(
        WorkspaceRequest(task_id="task-31", repo_url=str(source_repo))
    )

    runner = DockerSandboxRunner(default_image=image)
    result = runner.run(
        DockerSandboxCommand(
            workspace=workspace,
            command=[
                "python3",
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('docker-output.txt').write_text(Path('README.md').read_text()); "
                    "print(Path('README.md').read_text().strip())"
                ),
            ],
        )
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.strip() == "sandbox runner"
    assert (workspace.repo_path / "docker-output.txt").read_text(encoding="utf-8") == (
        "sandbox runner\n"
    )
