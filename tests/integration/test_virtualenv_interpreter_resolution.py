"""Integration tests for dynamic virtualenv interpreter resolution in DockerNativeAgentExecutor."""

from __future__ import annotations

from pathlib import Path

from sandbox.capability import CapabilityGrantFactory, FileSystemAccessPolicy, NetworkEgressPolicy
from sandbox.native_agent_executor import (
    DockerNativeAgentExecutor,
    native_executor_workspace_handle,
)
from sandbox.provider_bootstrap import ProviderBootstrap
from sandbox.secrets import SecretRegistry, SecretResolver
from sandbox.trusted_context import TrustedSandboxExecutionContext
from sandbox.workspace import WorkspaceHandle


def _make_trusted_context(task_id: str) -> TrustedSandboxExecutionContext:
    registry = SecretRegistry([])
    grant = CapabilityGrantFactory(registry).create_grant(
        network=NetworkEgressPolicy.DISABLED,
        filesystem=FileSystemAccessPolicy.WORKSPACE_WRITE,
        allowed_secret_refs=(),
        granted_secret_scopes=(),
    )
    return TrustedSandboxExecutionContext(
        grant=grant,
        task_id=task_id,
        provider_bootstrap=ProviderBootstrap(
            definitions=[], destination_by_ref={}, file_store={}, ref_names=()
        ),
        secret_resolver=SecretResolver(registry, file_store={}),
    )


def _setup_mock_repo(tmp_path: Path, *, create_venv: bool = True) -> tuple[WorkspaceHandle, Path]:
    workspace_root = tmp_path / "workspace"
    repo_dir = workspace_root / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    workspace_root.joinpath(".git").mkdir(exist_ok=True)
    workspace_root.joinpath(".git", "config").write_text("", encoding="utf-8")

    pytest_bin = repo_dir / ".venv" / "bin" / "pytest"
    if create_venv:
        pytest_bin.parent.mkdir(parents=True, exist_ok=True)
        pytest_bin.write_text("#!/bin/sh\necho 'MOCK_PYTEST'\n", encoding="utf-8")
        pytest_bin.chmod(0o755)

    workspace = native_executor_workspace_handle(
        workspace_path=workspace_root,
        repo_path=repo_dir,
        task_id="task-venv-test",
    )
    return workspace, pytest_bin


def test_login_shell_resolves_repo_virtualenv(tmp_path: Path) -> None:
    """Test 1: The exact failing shell invocation (bash -lc) resolves .venv/bin/pytest."""
    workspace, pytest_bin = _setup_mock_repo(tmp_path, create_venv=True)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    context = _make_trusted_context("task-login-shell")

    execution = DockerNativeAgentExecutor().run(
        command=["bash", "-lc", "which pytest"],
        prompt=None,
        workspace=workspace,
        artifact_root=artifact_root,
        environment={},
        timeout_seconds=30,
        scratch_namespace="login-shell",
        cancel_requested=None,
        redactor=None,
        context=context,
    )
    assert execution.completed.returncode == 0, execution.completed.stderr
    assert execution.completed.stdout.strip() == str(pytest_bin)


def test_non_interactive_shell_resolves_repo_virtualenv(tmp_path: Path) -> None:
    """Test 2: Non-interactive subshell (bash -c) resolves .venv/bin/pytest via BASH_ENV."""
    workspace, pytest_bin = _setup_mock_repo(tmp_path, create_venv=True)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    context = _make_trusted_context("task-non-interactive")

    execution = DockerNativeAgentExecutor().run(
        command=["bash", "-c", "which pytest"],
        prompt=None,
        workspace=workspace,
        artifact_root=artifact_root,
        environment={},
        timeout_seconds=30,
        scratch_namespace="non-interactive",
        cancel_requested=None,
        redactor=None,
        context=context,
    )
    assert execution.completed.returncode == 0, execution.completed.stderr
    assert execution.completed.stdout.strip() == str(pytest_bin)


def test_subdirectory_resolves_repo_virtualenv(tmp_path: Path) -> None:
    """Test 3: Execution from a nested repository subdirectory resolves .venv/bin/pytest."""
    workspace, pytest_bin = _setup_mock_repo(tmp_path, create_venv=True)
    sub_dir = workspace.repo_path / "src" / "subpackage"
    sub_dir.mkdir(parents=True, exist_ok=True)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    context = _make_trusted_context("task-subdir")

    execution = DockerNativeAgentExecutor().run(
        command=["bash", "-lc", "cd src/subpackage && which pytest"],
        prompt=None,
        workspace=workspace,
        artifact_root=artifact_root,
        environment={},
        timeout_seconds=30,
        scratch_namespace="subdir",
        cancel_requested=None,
        redactor=None,
        context=context,
    )
    assert execution.completed.returncode == 0, execution.completed.stderr
    assert execution.completed.stdout.strip() == str(pytest_bin)


def test_virtualenv_created_after_task_startup_is_resolved(tmp_path: Path) -> None:
    """Test 4: Virtualenv created after startup is resolved by subsequent subshells."""
    workspace, pytest_bin = _setup_mock_repo(tmp_path, create_venv=False)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    context = _make_trusted_context("task-post-startup")

    # Command: verify pytest is initially absent, create it dynamically, then spawn subshell
    script = (
        "! which pytest && "
        "mkdir -p .venv/bin && "
        "echo '#!/bin/sh\necho VENV_PYTEST\n' > .venv/bin/pytest && "
        "chmod +x .venv/bin/pytest && "
        "bash -lc 'which pytest'"
    )
    execution = DockerNativeAgentExecutor().run(
        command=["bash", "-lc", script],
        prompt=None,
        workspace=workspace,
        artifact_root=artifact_root,
        environment={},
        timeout_seconds=30,
        scratch_namespace="post-startup",
        cancel_requested=None,
        redactor=None,
        context=context,
    )
    assert execution.completed.returncode == 0, execution.completed.stderr
    assert execution.completed.stdout.strip() == str(pytest_bin)


def test_no_virtualenv_fallback_preserves_path(tmp_path: Path) -> None:
    """Test 5: When no virtualenv exists, standard PATH behavior is cleanly preserved."""
    workspace, _ = _setup_mock_repo(tmp_path, create_venv=False)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    context = _make_trusted_context("task-fallback")

    execution = DockerNativeAgentExecutor().run(
        command=["bash", "-lc", "which python3 && (! which pytest)"],
        prompt=None,
        workspace=workspace,
        artifact_root=artifact_root,
        environment={},
        timeout_seconds=30,
        scratch_namespace="fallback",
        cancel_requested=None,
        redactor=None,
        context=context,
    )
    assert execution.completed.returncode == 0, execution.completed.stderr
    assert "python3" in execution.completed.stdout
