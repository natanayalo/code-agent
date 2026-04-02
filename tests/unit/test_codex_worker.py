"""Unit tests for the Codex worker adapter."""

from __future__ import annotations

from pathlib import Path

from sandbox import (
    DockerSandboxResult,
    SandboxArtifact,
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
)
from workers import CodexWorker, WorkerRequest
from workers.codex_worker import (
    DEFAULT_WORKSPACE_ROOT_ENV_VAR,
    _default_workspace_root,
)


class FakeWorkspaceManager:
    """Capture workspace requests and return a predefined handle."""

    def __init__(self, workspace: WorkspaceHandle) -> None:
        self.workspace = workspace
        self.requests: list[object] = []

    def create_workspace(self, request: object) -> WorkspaceHandle:
        self.requests.append(request)
        return self.workspace


class FakeSandboxRunner:
    """Return a predefined sandbox result or raise an injected error."""

    def __init__(
        self,
        *,
        result: DockerSandboxResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.requests: list[object] = []

    def run(self, request: object) -> DockerSandboxResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _workspace_handle(tmp_path: Path) -> WorkspaceHandle:
    workspace_path = tmp_path / "workspace-task-41"
    repo_path = workspace_path / "repo"
    repo_path.mkdir(parents=True)
    return WorkspaceHandle(
        workspace_id="workspace-task-41",
        task_id="task-41",
        workspace_path=workspace_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        branch="main",
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )


def test_codex_worker_requires_repo_url(tmp_path: Path) -> None:
    """The real worker should fail fast when it cannot provision a repo workspace."""
    workspace = _workspace_handle(tmp_path)
    workspace_manager = FakeWorkspaceManager(workspace)
    sandbox_runner = FakeSandboxRunner()
    worker = CodexWorker(workspace_manager=workspace_manager, sandbox_runner=sandbox_runner)

    result = worker.run(WorkerRequest(task_text="Summarize the repo"))

    assert result.status == "error"
    assert result.summary == "CodexWorker requires repo_url to provision a sandbox workspace."
    assert result.next_action_hint == "provide_repo_url"
    assert workspace_manager.requests == []
    assert sandbox_runner.requests == []


def test_default_workspace_root_supports_environment_override(
    monkeypatch,
) -> None:
    """The default workspace root should be configurable per environment."""
    monkeypatch.setenv(DEFAULT_WORKSPACE_ROOT_ENV_VAR, "~/custom-codex-root")

    assert _default_workspace_root() == Path("~/custom-codex-root").expanduser()


def test_codex_worker_maps_sandbox_result_into_worker_contract(tmp_path: Path) -> None:
    """A successful sandbox run should be exposed through the shared worker contract."""
    workspace = _workspace_handle(tmp_path)
    workspace_manager = FakeWorkspaceManager(workspace)
    sandbox_runner = FakeSandboxRunner(
        result=DockerSandboxResult(
            image="python:3.12-slim",
            command=["python3", "/workspace/.code-agent/codex_worker_task.py"],
            docker_command=["docker", "run", "python:3.12-slim"],
            exit_code=0,
            stdout="Wrote .code-agent/codex-worker-report.md\n",
            stderr="",
            duration_seconds=1.5,
            files_changed=[".code-agent/codex-worker-report.md"],
            artifacts=[
                SandboxArtifact(
                    name="stdout.log",
                    uri="artifacts/command-123/stdout.log",
                    artifact_type="log",
                )
            ],
        )
    )
    worker = CodexWorker(workspace_manager=workspace_manager, sandbox_runner=sandbox_runner)

    result = worker.run(
        WorkerRequest(
            session_id="session-41",
            repo_url="https://example.com/repo.git",
            branch="main",
            task_text="Summarize the repo",
            memory_context={"project": [{"memory_key": "pitfall"}]},
            constraints={"requires_approval": False},
            budget={"max_minutes": 5},
        )
    )

    assert result.status == "success"
    assert result.summary == (
        "CodexWorker completed a sandboxed toy repo task and retained the workspace."
    )
    assert result.commands_run[0].exit_code == 0
    assert result.files_changed == [".code-agent/codex-worker-report.md"]
    assert result.test_results[0].status == "passed"
    assert result.artifacts[0].artifact_type == "workspace"
    assert result.artifacts[0].uri == str(workspace.workspace_path)
    assert result.artifacts[1].uri == "artifacts/command-123/stdout.log"

    workspace_request = workspace_manager.requests[0]
    assert getattr(workspace_request, "cleanup_policy").delete_on_success is False
    assert getattr(workspace_request, "cleanup_policy").retain_on_failure is True

    sandbox_request = sandbox_runner.requests[0]
    assert getattr(sandbox_request, "command") == [
        "python3",
        "/workspace/.code-agent/codex_worker_task.py",
    ]
    assert getattr(sandbox_request, "working_dir") == "/workspace/repo"
    assert getattr(sandbox_request, "environment")["TASK_TEXT"] == "Summarize the repo"

    script_path = workspace.workspace_path / ".code-agent" / "codex_worker_task.py"
    assert script_path.exists()
    assert script_path.parent == (workspace.workspace_path / ".code-agent")
    assert not (workspace.repo_path / ".code-agent" / "codex_worker_task.py").exists()
