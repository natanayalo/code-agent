"""Unit tests for the Codex worker adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path

import workers.codex_worker as codex_worker_module
from sandbox import (
    DockerSandboxResult,
    SandboxArtifact,
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
)
from workers import CodexWorker, WorkerRequest
from workers.codex_worker import (
    DEFAULT_WORKSPACE_ROOT_ENV_VAR,
    _build_test_result_details,
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


class RaisingWorkspaceManager:
    """Raise a predefined workspace creation error."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def create_workspace(self, request: object) -> WorkspaceHandle:
        raise self.error


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

    result = asyncio.run(worker.run(WorkerRequest(task_text="Summarize the repo")))

    assert result.status == "error"
    assert result.summary == (
        "CodexWorker requires a non-empty repo_url to provision a sandbox workspace."
    )
    assert result.next_action_hint == "provide_repo_url"
    assert workspace_manager.requests == []
    assert sandbox_runner.requests == []


def test_codex_worker_rejects_blank_repo_url(tmp_path: Path) -> None:
    """Blank repository URLs should be rejected before workspace validation."""
    workspace = _workspace_handle(tmp_path)
    workspace_manager = FakeWorkspaceManager(workspace)
    sandbox_runner = FakeSandboxRunner()
    worker = CodexWorker(workspace_manager=workspace_manager, sandbox_runner=sandbox_runner)

    result = asyncio.run(worker.run(WorkerRequest(task_text="Summarize the repo", repo_url="   ")))

    assert result.status == "error"
    assert result.summary == (
        "CodexWorker requires a non-empty repo_url to provision a sandbox workspace."
    )
    assert result.next_action_hint == "provide_repo_url"
    assert workspace_manager.requests == []
    assert sandbox_runner.requests == []


def test_default_workspace_root_supports_environment_override(
    monkeypatch,
) -> None:
    """The default workspace root should be configurable per environment."""
    monkeypatch.setenv(DEFAULT_WORKSPACE_ROOT_ENV_VAR, "~/custom-codex-root")

    assert _default_workspace_root() == Path("~/custom-codex-root").expanduser()


def test_default_workspace_root_uses_uid_scoped_temp_dir(
    monkeypatch,
) -> None:
    """The fallback workspace root should include the local uid to reduce collisions."""
    monkeypatch.delenv(DEFAULT_WORKSPACE_ROOT_ENV_VAR, raising=False)
    monkeypatch.setattr(codex_worker_module.os, "getuid", lambda: 4242)

    assert _default_workspace_root() == (
        Path(tempfile.gettempdir()) / "code-agent-workspaces-uid-4242"
    )


def test_default_workspace_root_falls_back_to_pid_scoped_temp_dir(
    monkeypatch,
) -> None:
    """The last-resort fallback should avoid a shared temp directory name."""
    monkeypatch.delenv(DEFAULT_WORKSPACE_ROOT_ENV_VAR, raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.setattr(codex_worker_module.os, "getuid", None, raising=False)
    monkeypatch.setattr(codex_worker_module.os, "getpid", lambda: 31337)

    assert _default_workspace_root() == (
        Path(tempfile.gettempdir()) / "code-agent-workspaces-pid-31337"
    )


def test_build_test_result_details_prefers_stderr_on_failure() -> None:
    """Failure summaries should surface stderr before stdout."""
    details = _build_test_result_details(
        exit_code=1,
        stdout="partial progress\n",
        stderr="Traceback: boom\n",
    )

    assert details == "STDERR:\nTraceback: boom\n\nSTDOUT:\npartial progress"


def test_codex_worker_masks_repo_url_in_logs_and_context(
    tmp_path: Path,
    caplog,
) -> None:
    """Credential-bearing repo URLs should be masked outside the clone request itself."""
    workspace = _workspace_handle(tmp_path)
    workspace_manager = FakeWorkspaceManager(workspace)
    sandbox_runner = FakeSandboxRunner(
        result=DockerSandboxResult(
            image="python:3.12-slim",
            command=[
                "python3",
                "/workspace/.code-agent/codex_worker_task.py",
                "/workspace/.code-agent/codex_worker_context.json",
            ],
            docker_command=["docker", "run", "python:3.12-slim"],
            exit_code=0,
            stdout="Wrote .code-agent/codex-worker-report.md\n",
            stderr="",
            duration_seconds=1.5,
            files_changed=[".code-agent/codex-worker-report.md"],
            artifacts=[],
        )
    )
    worker = CodexWorker(workspace_manager=workspace_manager, sandbox_runner=sandbox_runner)

    with caplog.at_level(logging.INFO, logger="workers.codex_worker"):
        asyncio.run(
            worker.run(
                WorkerRequest(
                    session_id="session-41",
                    repo_url="https://token@github.com/example/repo.git",
                    branch="main",
                    task_text="Summarize the repo",
                )
            )
        )

    workspace_request = workspace_manager.requests[0]
    assert getattr(workspace_request, "repo_url") == "https://token@github.com/example/repo.git"

    start_record = next(
        record for record in caplog.records if record.getMessage() == "Starting Codex worker run"
    )
    assert getattr(start_record, "repo_url") == "https://****@github.com/example/repo.git"

    context_path = workspace.workspace_path / ".code-agent" / "codex_worker_context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context["repo_url"] == "https://****@github.com/example/repo.git"


def test_codex_worker_handles_workspace_permission_error() -> None:
    """Workspace-permission failures should become structured worker errors."""
    worker = CodexWorker(
        workspace_manager=RaisingWorkspaceManager(PermissionError("permission denied")),
        sandbox_runner=FakeSandboxRunner(),
    )

    result = asyncio.run(
        worker.run(
            WorkerRequest(
                session_id="session-41",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Summarize the repo",
            )
        )
    )

    assert result.status == "error"
    assert result.summary == ("CodexWorker failed to provision a workspace: permission denied")
    assert result.next_action_hint == "inspect_worker_configuration"


def test_codex_worker_maps_sandbox_result_into_worker_contract(tmp_path: Path) -> None:
    """A successful sandbox run should be exposed through the shared worker contract."""
    workspace = _workspace_handle(tmp_path)
    workspace_manager = FakeWorkspaceManager(workspace)
    sandbox_runner = FakeSandboxRunner(
        result=DockerSandboxResult(
            image="python:3.12-slim",
            command=[
                "python3",
                "/workspace/.code-agent/codex_worker_task.py",
                "/workspace/.code-agent/codex_worker_context.json",
            ],
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

    result = asyncio.run(
        worker.run(
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
        "/workspace/.code-agent/codex_worker_context.json",
    ]
    assert getattr(sandbox_request, "working_dir") == "/workspace/repo"
    assert getattr(sandbox_request, "environment") == {}

    script_path = workspace.workspace_path / ".code-agent" / "codex_worker_task.py"
    context_path = workspace.workspace_path / ".code-agent" / "codex_worker_context.json"
    assert script_path.exists()
    assert context_path.exists()
    assert script_path.parent == (workspace.workspace_path / ".code-agent")
    assert not (workspace.repo_path / ".code-agent" / "codex_worker_task.py").exists()
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context == {
        "branch": "main",
        "budget": {"max_minutes": 5},
        "constraints": {"requires_approval": False},
        "memory_context": {"project": [{"memory_key": "pitfall"}]},
        "repo_url": "https://example.com/repo.git",
        "session_id": "session-41",
        "task_text": "Summarize the repo",
    }


def test_codex_worker_failure_result_includes_stderr_details(tmp_path: Path) -> None:
    """Failed toy task runs should expose stderr-rich details in the test result."""
    workspace = _workspace_handle(tmp_path)
    worker = CodexWorker(
        workspace_manager=FakeWorkspaceManager(workspace),
        sandbox_runner=FakeSandboxRunner(
            result=DockerSandboxResult(
                image="python:3.12-slim",
                command=[
                    "python3",
                    "/workspace/.code-agent/codex_worker_task.py",
                    "/workspace/.code-agent/codex_worker_context.json",
                ],
                docker_command=["docker", "run", "python:3.12-slim"],
                exit_code=1,
                stdout="partial progress\n",
                stderr="Traceback: boom\n",
                duration_seconds=1.5,
                files_changed=[],
                artifacts=[],
            )
        ),
    )

    result = asyncio.run(
        worker.run(
            WorkerRequest(
                session_id="session-41",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Summarize the repo",
            )
        )
    )

    assert result.status == "failure"
    assert result.test_results[0].status == "failed"
    assert result.test_results[0].details == (
        "STDERR:\nTraceback: boom\n\nSTDOUT:\npartial progress"
    )


def test_codex_worker_rejects_unserializable_execution_context(tmp_path: Path) -> None:
    """Unsupported context values should fail explicitly instead of being stringified."""
    workspace = _workspace_handle(tmp_path)
    worker = CodexWorker(
        workspace_manager=FakeWorkspaceManager(workspace),
        sandbox_runner=FakeSandboxRunner(),
    )

    class Unserializable:
        pass

    result = asyncio.run(
        worker.run(
            WorkerRequest(
                session_id="session-41",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Summarize the repo",
                memory_context={"project": [Unserializable()]},
            )
        )
    )

    assert result.status == "error"
    assert result.summary == (
        "CodexWorker failed to serialize the sandbox execution context: "
        "memory_context.project[0] contains unsupported value of type Unserializable."
    )
    assert result.next_action_hint == "inspect_worker_configuration"
    assert result.artifacts[0].artifact_type == "workspace"
    assert result.artifacts[0].uri == str(workspace.workspace_path)
