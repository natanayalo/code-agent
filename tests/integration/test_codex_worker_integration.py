"""Integration tests for the Codex worker implementation."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from orchestrator import OrchestratorState, build_orchestrator_graph
from sandbox import (
    DockerSandboxRunner,
    SecretRedactor,
    WorkspaceCleanupPolicy,
    WorkspaceManager,
)
from workers import CodexWorker, WorkerRequest


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
    (repo_path / "README.md").write_text("integration test repo\n", encoding="utf-8")
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


def _workspace_path_from_docker_command(command: list[str]) -> Path:
    mount_index = command.index("--mount") + 1
    mount_spec = command[mount_index]
    for segment in mount_spec.split(","):
        if segment.startswith("source="):
            return Path(segment.removeprefix("source="))
    raise AssertionError(f"Mount source missing from docker command: {command}")


def test_codex_worker_runs_real_workspace_and_graph_path(tmp_path: Path) -> None:
    """The orchestrator can invoke the real Codex worker through the shared contract."""
    source_repo = _create_local_repo(tmp_path)
    captured_command: list[str] = []

    def fake_docker_command_runner(
        command: list[str], *, timeout: int, redactor: SecretRedactor | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == 300
        captured_command[:] = command
        workspace_path = _workspace_path_from_docker_command(command)
        report_path = workspace_path / "repo" / ".code-agent" / "codex-worker-report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("# Codex Worker Report\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="Wrote .code-agent/codex-worker-report.md\n",
            stderr="",
        )

    worker = CodexWorker(
        workspace_manager=WorkspaceManager(tmp_path / "workspaces"),
        sandbox_runner=DockerSandboxRunner(command_runner=fake_docker_command_runner),
    )
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Summarize the repo state",
                    "repo_url": str(source_repo),
                    "branch": "main",
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert state.route.chosen_worker == "codex"
    assert state.dispatch.worker_type == "codex"
    assert state.dispatch.run_id is None
    assert state.dispatch.workspace_id is None
    assert state.result is not None
    assert state.result.status == "success"
    assert ".code-agent/codex-worker-report.md" in state.result.files_changed
    assert state.result.summary == (
        "CodexWorker completed a sandboxed toy repo task and retained the workspace."
    )
    assert captured_command[-3:] == [
        "python3",
        "/workspace/.code-agent/codex_worker_task.py",
        "/workspace/.code-agent/codex_worker_context.json",
    ]

    artifact_names = {artifact.name for artifact in state.result.artifacts}
    assert "workspace" in artifact_names
    assert "stdout.log" in artifact_names
    assert "changed-files.txt" in artifact_names
    stdout_artifact = next(
        artifact for artifact in state.result.artifacts if artifact.name == "stdout.log"
    )
    assert (
        stdout_artifact.uri == "artifacts/command-123/stdout.log"
        or stdout_artifact.uri.startswith("artifacts/command-")
    )

    workspace_artifact = next(
        artifact for artifact in state.result.artifacts if artifact.name == "workspace"
    )
    workspace_path = Path(workspace_artifact.uri)
    assert (workspace_path / "repo" / ".code-agent" / "codex-worker-report.md").exists()
    assert (workspace_path / ".code-agent" / "codex_worker_task.py").exists()
    context_path = workspace_path / ".code-agent" / "codex_worker_context.json"
    assert context_path.exists()
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context["task_text"] == "Summarize the repo state"
    assert context["repo_url"] == str(source_repo)
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "memory context loaded",
        "worker selected: codex",
        "approval not required",
        "worker dispatched",
        "worker result received",
        "verification passed",
        "result summarized",
        "memory persistence queued",
    ]


def test_codex_worker_honors_delete_on_success_cleanup_policy(tmp_path: Path) -> None:
    """A caller-provided cleanup policy should be enforced after a successful run."""
    source_repo = _create_local_repo(tmp_path)

    def fake_docker_command_runner(
        command: list[str], *, timeout: int, redactor: SecretRedactor | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == 300
        workspace_path = _workspace_path_from_docker_command(command)
        report_path = workspace_path / "repo" / ".code-agent" / "codex-worker-report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("# Codex Worker Report\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="Wrote .code-agent/codex-worker-report.md\n",
            stderr="",
        )

    worker = CodexWorker(
        workspace_manager=WorkspaceManager(tmp_path / "workspaces"),
        sandbox_runner=DockerSandboxRunner(command_runner=fake_docker_command_runner),
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=True, retain_on_failure=True),
    )

    result = asyncio.run(
        worker.run(
            WorkerRequest(
                session_id="session-41",
                repo_url=str(source_repo),
                branch="main",
                task_text="Summarize the repo state",
            )
        )
    )

    assert result.status == "success"
    assert (
        result.summary
        == "CodexWorker completed a sandboxed toy repo task and cleaned up the workspace."
    )
    assert result.artifacts == []
    assert result.next_action_hint is None
