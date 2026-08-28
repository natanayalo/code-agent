import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import workers.native_agent_runner as native_agent_runner
from db.enums import WorkerRuntimeMode
from sandbox.capability import CapabilityGrantFactory, FileSystemAccessPolicy, NetworkEgressPolicy
from sandbox.native_agent_executor import (
    DockerNativeAgentExecutor,
    native_agent_home_for_request,
    sandbox_file_secret_dir_for_request,
)
from sandbox.provider_bootstrap import ProviderBootstrap
from sandbox.secrets import (
    RegisteredSecretDefinition,
    SecretExposurePolicy,
    SecretRegistry,
    SecretResolver,
    SecretScope,
    SecretSource,
)
from sandbox.trusted_context import TrustedSandboxExecutionContext
from tests.native_agent_test_doubles import LocalNativeAgentRunner
from tests.unit.test_gemini_cli_worker import (
    _FakeContainerManager,
    _FakeWorkspaceManager,
    _make_container,
    _make_workspace,
)
from workers.base import WorkerRequest
from workers.gemini_cli_worker import GeminiCliWorker


@pytest.mark.asyncio
async def test_native_agent_integration_flow(tmp_path: Path):
    """Verify integration between GeminiCliWorker and NativeAgentRunner."""
    workspace = _make_workspace(tmp_path)

    # Create a fake Gemini CLI binary
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gemini_bin = bin_dir / "gemini"

    script = """#!/usr/bin/env python3
import json
import sys

# Simulate Gemini CLI outputting JSON to stdout
response = {
    "response": "Refactor complete via native CLI",
    "stats": {"tokens": 100}
}
print(json.dumps(response))
sys.exit(0)
"""
    gemini_bin.write_text(script, encoding="utf-8")
    gemini_bin.chmod(gemini_bin.stat().st_mode | stat.S_IEXEC)

    worker = GeminiCliWorker(
        runtime_adapter=MagicMock(env={}, executable="gemini", model="gemini-pro"),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(_make_container(workspace)),
        # The fake provider is intentionally host-local, so this test explicitly
        # injects a test runner rather than relying on production Docker behavior.
    )

    # We need to ensure the worker uses our fake binary.
    # GeminiCliWorker builds the command using 'gemini'.
    # We'll patch the environment used by run_native_agent.

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(native_agent_runner, "DockerNativeAgentExecutor", LocalNativeAgentRunner)

        from sandbox.provider_bootstrap import ProviderBootstrap

        mp.setattr(
            "sandbox.provider_bootstrap.ProviderBootstrapLoader.load",
            MagicMock(return_value=ProviderBootstrap([], {}, {}, ())),
        )
        current_path = os.environ.get("PATH", "")
        new_path = f"{bin_dir}{os.pathsep}{tmp_path}"
        if current_path:
            new_path += f"{os.pathsep}{current_path}"
        mp.setenv("PATH", new_path)

        request = WorkerRequest(
            task_text="Refactor this",
            repo_url="https://example.com/repo.git",
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            # We can't easily override the binary name in GeminiCliWorker without patching
        )

        with patch.object(
            GeminiCliWorker,
            "_build_native_command",
            return_value=[str(gemini_bin), "--output-format", "json"],
        ):
            result = await worker.run(request)

        if result.status != "success":
            msg = f"Worker failed with status {result.status}: {result.summary}"
            pytest.fail(msg)
        assert result.summary == "Refactor complete via native CLI"
        assert result.budget_usage["runtime_mode"] == "native_agent"


@pytest.mark.asyncio
async def test_native_agent_integration_failure_mapping(tmp_path: Path):
    """Verify that native agent failures are correctly mapped to FailureKind."""
    workspace = _make_workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gemini_bin = bin_dir / "gemini"

    # Mock a provider error
    script = """#!/usr/bin/env python3
import json
import sys
response = {
    "error": {
        "type": "rate_limit_exceeded",
        "message": "Too many requests"
    }
}
print(json.dumps(response))
sys.exit(1)
"""
    gemini_bin.write_text(script, encoding="utf-8")
    gemini_bin.chmod(gemini_bin.stat().st_mode | stat.S_IEXEC)

    worker = GeminiCliWorker(
        runtime_adapter=MagicMock(env={}, executable="gemini", model="gemini-pro"),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(_make_container(workspace)),
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(native_agent_runner, "DockerNativeAgentExecutor", LocalNativeAgentRunner)

        from sandbox.provider_bootstrap import ProviderBootstrap

        mp.setattr(
            "sandbox.provider_bootstrap.ProviderBootstrapLoader.load",
            MagicMock(return_value=ProviderBootstrap([], {}, {}, ())),
        )
        current_path = os.environ.get("PATH", "")
        new_path = f"{bin_dir}{os.pathsep}{tmp_path}"
        if current_path:
            new_path += f"{os.pathsep}{current_path}"
        mp.setenv("PATH", new_path)

        request = WorkerRequest(
            task_text="Refactor this",
            repo_url="https://example.com/repo.git",
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        )

        with patch.object(
            GeminiCliWorker,
            "_build_native_command",
            return_value=[str(gemini_bin), "--output-format", "json"],
        ):
            result = await worker.run(request)

        if result.status != "failure":
            msg = f"Worker failed with status {result.status}: {result.summary}"
            pytest.fail(msg)
        assert "rate_limit_exceeded" in result.summary
        # The failure kind should be mapped by classify_failure_kind
        # Rate limits are usually mapped to 'provider_error' or similar
        assert result.failure_kind is not None


def _is_docker_available() -> bool:
    try:
        docker_info = subprocess.run(
            ["docker", "info"], capture_output=True, check=False, timeout=5
        )
        if docker_info.returncode != 0:
            return False
        inspect = subprocess.run(
            ["docker", "image", "inspect", "code-agent-worker:latest"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        return inspect.returncode == 0
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _is_docker_available(), reason="Docker or code-agent-worker:latest unavailable"
)
async def test_native_agent_mutation_with_scratch_namespace(tmp_path: Path):
    """Verify that a native agent can mutate the workspace when scratch_namespace is used and read_only is False."""  # noqa: E501
    from sandbox.capability import (
        CapabilityGrantFactory,
        FileSystemAccessPolicy,
        NetworkEgressPolicy,
    )
    from sandbox.provider_bootstrap import ProviderBootstrap
    from sandbox.secrets import InMemoryEphemeralSecretStore, SecretRegistry
    from sandbox.trusted_context import TrustedSandboxExecutionContext
    from tests.unit.test_gemini_cli_worker import _make_workspace
    from workers.native_agent_runner import DockerNativeAgentExecutor

    workspace = _make_workspace(tmp_path)
    workspace.workspace_path.joinpath("target.txt").write_text("initial", encoding="utf-8")
    workspace.workspace_path.joinpath(".git").mkdir(exist_ok=True)
    workspace.workspace_path.joinpath(".git", "config").write_text("", encoding="utf-8")

    # We want a real docker execution that mutates the file
    registry = SecretRegistry(ephemeral_store=InMemoryEphemeralSecretStore(), task_id="task-123")
    grant = CapabilityGrantFactory(registry).create_grant(
        network=NetworkEgressPolicy.DISABLED, filesystem=FileSystemAccessPolicy.WORKSPACE_WRITE
    )

    context = TrustedSandboxExecutionContext(
        grant=grant,
        task_id="task-123",
        provider_bootstrap=ProviderBootstrap(
            definitions=[], destination_by_ref={}, file_store={}, ref_names=()
        ),
        secret_resolver=MagicMock(),
    )

    executor = DockerNativeAgentExecutor()

    # Run a simple shell command that mutates the target file.
    execution = executor.run(
        command=["sh", "-c", "echo 'mutated' > target.txt"],
        prompt=None,
        workspace=workspace,
        artifact_root=tmp_path / "artifacts",
        environment={},
        timeout_seconds=30,
        scratch_namespace="test-scratch",
        cancel_requested=None,
        redactor=None,
        context=context,
    )

    assert execution.completed.returncode == 0
    assert (
        workspace.repo_path.joinpath("target.txt").read_text(encoding="utf-8").strip() == "mutated"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _is_docker_available(), reason="Docker or code-agent-worker:latest unavailable"
)
async def test_native_agent_mounts_file_secret_at_declared_destination(tmp_path: Path):
    """File-backed secrets must be readable only at their broker-declared path."""
    workspace = _make_workspace(tmp_path)
    workspace.workspace_path.joinpath(".git").mkdir(exist_ok=True)
    workspace.workspace_path.joinpath(".git", "config").write_text("", encoding="utf-8")
    definition = RegisteredSecretDefinition(
        name="task_certificate",
        source=SecretSource.FILE,
        source_key="certificate.pem",
        required_scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_FILE,
        destination_mount_path="declared-certificate.pem",
    )
    registry = SecretRegistry([definition])
    grant = CapabilityGrantFactory(registry).create_grant(
        network=NetworkEgressPolicy.DISABLED,
        filesystem=FileSystemAccessPolicy.WORKSPACE_WRITE,
        allowed_secret_refs=("task_certificate",),
        granted_secret_scopes=(SecretScope.CUSTOM,),
    )
    context = TrustedSandboxExecutionContext(
        grant=grant,
        task_id="task-file-secret",
        provider_bootstrap=ProviderBootstrap(
            definitions=[], destination_by_ref={}, file_store={}, ref_names=()
        ),
        secret_resolver=SecretResolver(
            registry, file_store={"certificate.pem": "test-certificate"}
        ),
    )
    agent_home = native_agent_home_for_request(workspace.workspace_path, "file-secret")
    sandbox_secrets_dir = sandbox_file_secret_dir_for_request(
        workspace.workspace_path, "file-secret"
    )
    artifact_root = tmp_path / "artifacts"
    execution = DockerNativeAgentExecutor().run(
        command=[
            "sh",
            "-c",
            'test "$(cat /run/secrets/code-agent/declared-certificate.pem)" = test-certificate '
            '&& test "$(stat -c %a /run/secrets/code-agent/declared-certificate.pem)" = 440 '
            '&& test ! -e "$HOME/secrets/declared-certificate.pem" '
            "&& ! (printf replacement > /run/secrets/code-agent/declared-certificate.pem)",
        ],
        prompt=None,
        workspace=workspace,
        artifact_root=artifact_root,
        environment={},
        timeout_seconds=30,
        scratch_namespace="file-secret",
        cancel_requested=None,
        redactor=None,
        context=context,
    )

    assert execution.completed.returncode == 0, execution.completed.stderr
    assert not agent_home.exists()
    secret_mount = next(
        mount
        for mount in execution.completed.args
        if mount.endswith("target=/run/secrets/code-agent,readonly")
    )
    secret_source = Path(secret_mount.split("source=", 1)[1].split(",target=", 1)[0])
    assert secret_source == sandbox_secrets_dir.resolve()
    assert not secret_source.is_relative_to(workspace.workspace_path)
    assert not secret_source.is_relative_to(agent_home)
    assert not secret_source.is_relative_to(artifact_root)
    assert not secret_source.exists()
