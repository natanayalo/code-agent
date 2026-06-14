"""Integration tests for the Docker sandbox runner."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from sandbox import (
    DockerSandboxCommand,
    DockerSandboxContainerManager,
    DockerSandboxContainerRequest,
    DockerSandboxRunner,
    DockerShellSession,
    WorkspaceManager,
    WorkspaceRequest,
)


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
    assert result.files_changed == ["docker-output.txt"]
    artifact_paths = {
        artifact.name: workspace.workspace_path / artifact.uri for artifact in result.artifacts
    }
    assert set(artifact_paths) == {
        "stdout.log",
        "stderr.log",
        "changed-files.txt",
        "diff-summary.txt",
    }
    assert artifact_paths["stdout.log"].read_text(encoding="utf-8").strip() == "sandbox runner"
    assert artifact_paths["stderr.log"].read_text(encoding="utf-8") == ""
    assert artifact_paths["changed-files.txt"].read_text(encoding="utf-8") == "docker-output.txt\n"
    diff_summary = artifact_paths["diff-summary.txt"].read_text(encoding="utf-8")
    assert "Untracked files:" in diff_summary
    assert "- docker-output.txt" in diff_summary
    assert (workspace.repo_path / "docker-output.txt").read_text(encoding="utf-8") == (
        "sandbox runner\n"
    )


def test_persistent_shell_session_preserves_state_across_commands(tmp_path: Path) -> None:
    """A long-lived shell session should preserve env, cwd, and filesystem state."""
    image = os.environ.get("CODE_AGENT_TEST_DOCKER_IMAGE", "python:3.12-slim")
    if not _docker_and_image_available(image):
        pytest.skip(f"Docker daemon or image {image!r} is unavailable")

    source_repo = _create_local_repo(tmp_path)
    workspace_manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = workspace_manager.create_workspace(
        WorkspaceRequest(task_id="task-45", repo_url=str(source_repo))
    )

    container_manager = DockerSandboxContainerManager(default_image=image)
    container = container_manager.start(DockerSandboxContainerRequest(workspace=workspace))

    try:
        reconnected = container_manager.reconnect(container)
        assert reconnected.container_name == container.container_name

        with DockerShellSession(container) as session:
            first = session.execute("export GREETING=hello; cd .git")
            second = session.execute('printf "%s:%s\\n" "$GREETING" "$(pwd)"')
            third = session.execute("cd ..; printf 'notes\\n' > session-output.txt")
            fourth = session.execute("cat session-output.txt")

        assert first.exit_code == 0
        assert second.exit_code == 0
        assert second.output.strip() == f"hello:{workspace.repo_path.resolve()}/.git"
        assert third.exit_code == 0
        assert fourth.output == "notes\n"
        assert (workspace.repo_path / "session-output.txt").read_text(encoding="utf-8") == (
            "notes\n"
        )
    finally:
        container_manager.stop(container)


def test_docker_sandbox_runner_read_only_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mounted read-only workspace command should fail to write outside explicit mounts."""
    image = os.environ.get("CODE_AGENT_TEST_DOCKER_IMAGE", "python:3.12-slim")
    if not _docker_and_image_available(image):
        pytest.skip(f"Docker daemon or image {image!r} is unavailable")

    source_repo = _create_local_repo(tmp_path)
    workspace_manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = workspace_manager.create_workspace(
        WorkspaceRequest(task_id="task-ro", repo_url=str(source_repo))
    )

    runner = DockerSandboxRunner(default_image=image)

    # 1. Attempt to write to the root of the workspace should fail
    result_fail = runner.run(
        DockerSandboxCommand(
            workspace=workspace,
            read_only_workspace=True,
            command=[
                "python3",
                "-c",
                "from pathlib import Path; Path('should_fail.txt').write_text('fail')",
            ],
        )
    )
    assert result_fail.exit_code != 0

    # 2. Attempt to write to .code-agent or artifacts should succeed
    result_success = runner.run(
        DockerSandboxCommand(
            workspace=workspace,
            read_only_workspace=True,
            command=[
                "python3",
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('artifacts/should_succeed.txt').write_text('success')"
                ),
            ],
        )
    )
    assert result_success.exit_code == 0
    assert (workspace.workspace_path / "artifacts" / "should_succeed.txt").exists()

    # 3. Attempt to write to local repo should fail (using ContainerManager)
    monkeypatch.setenv("CODE_AGENT_ALLOWED_LOCAL_REMOTES", str(source_repo))
    workspace_with_local_repo = workspace_manager.create_workspace(
        WorkspaceRequest(task_id="task-ro-local", repo_url=f"file://{source_repo}")
    )
    container_manager = DockerSandboxContainerManager(default_image=image)
    container = container_manager.start(
        DockerSandboxContainerRequest(
            workspace=workspace_with_local_repo,
            read_only_workspace=True,
        )
    )
    try:
        with DockerShellSession(container) as session:
            python_cmd = (
                'python3 -c "from pathlib import Path; '
                f"Path('{source_repo.as_posix()}/local_repo_write.txt').write_text('fail')\""
            )
            result_local_repo = session.execute(python_cmd)
        assert result_local_repo.exit_code != 0
    finally:
        container_manager.stop(container)
