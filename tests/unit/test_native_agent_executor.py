"""Security regression tests for Docker-native agent isolation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import sandbox.native_agent_proxy as native_agent_proxy
from sandbox.capability import FileSystemAccessPolicy, NetworkEgressPolicy, SandboxCapabilityGrant
from sandbox.native_agent_executor import (
    DockerNativeAgentExecutor,
    NativeAgentExecutorError,
    build_executor_environment,
    is_public_egress_host,
    native_agent_home_for_request,
    native_executor_workspace_handle,
    provider_home_name,
    sandbox_file_secret_dir_for_request,
)
from sandbox.provider_bootstrap import ProviderBootstrap
from sandbox.scratch import node_agent_home, node_scratch_root
from sandbox.trusted_context import TrustedSandboxExecutionContext


def test_executor_command_is_hardened_and_mounts_only_task_paths(tmp_path: Path) -> None:
    workspace_root = tmp_path / "task"
    repo = workspace_root / "repo"
    artifacts = workspace_root / ".code-agent" / "artifacts"
    agent_home = tmp_path / "agent-home"
    repo.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    agent_home.mkdir()
    command = DockerNativeAgentExecutor(image="native-image").build_run_command(
        container_name="native-test",
        command=["codex", "exec"],
        workspace=native_executor_workspace_handle(
            workspace_path=workspace_root, repo_path=repo, task_id="task-1"
        ),
        artifact_root=artifacts,
        agent_home=agent_home,
        environment={"PATH": "/usr/bin"},
        read_only_workspace=True,
        resource_limits=type(
            "ResourceLimits",
            (),
            {"pids_limit": 100, "memory_bytes": 1024 * 1024 * 1024, "cpu_limit": 1.0},
        )(),
    )

    joined = " ".join(command)
    assert "--rm" not in command
    assert "--read-only" in command
    cap_drop_index = command.index("--cap-drop")
    assert ["--cap-drop", "ALL"] == command[cap_drop_index : cap_drop_index + 2]
    assert "no-new-privileges=true" in joined
    assert "--pids-limit" in command and "--memory" in command and "--cpus" in command
    assert ["--ipc", "private"] == command[command.index("--ipc") : command.index("--ipc") + 2]
    assert "/tmp:rw,noexec,nosuid,size=64m" in joined
    assert f"source={workspace_root.resolve()},target={workspace_root.resolve()},readonly" in joined
    runtime_root = (workspace_root / ".code-agent").resolve()
    assert f"source={runtime_root},target={runtime_root}" in joined
    assert f"source={artifacts.resolve()},target={artifacts.resolve()}" in joined
    assert f"source={agent_home.resolve()},target={agent_home.resolve()}" in joined
    assert "/var/run/docker.sock" not in joined
    assert "seccomp=unconfined" not in joined
    assert "--network none" in joined


def test_executor_command_mounts_file_secrets_at_the_declared_container_path(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "task"
    repo = workspace_root / "repo"
    artifacts = workspace_root / ".code-agent" / "artifacts"
    agent_home = tmp_path / "agent-home"
    secret_dir = tmp_path / "sandbox-secrets"
    repo.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    agent_home.mkdir()
    secret_dir.mkdir()
    command = DockerNativeAgentExecutor(image="native-image").build_run_command(
        container_name="native-test",
        command=["codex", "exec"],
        workspace=native_executor_workspace_handle(
            workspace_path=workspace_root, repo_path=repo, task_id="task-1"
        ),
        artifact_root=artifacts,
        agent_home=agent_home,
        sandbox_secrets_dir=secret_dir,
        environment={"PATH": "/usr/bin"},
        read_only_workspace=True,
        resource_limits=type(
            "ResourceLimits",
            (),
            {"pids_limit": 100, "memory_bytes": 1024 * 1024 * 1024, "cpu_limit": 1.0},
        )(),
    )

    assert (
        f"type=bind,source={secret_dir.resolve()},target=/run/secrets/code-agent,readonly"
        in command
    )


def test_executor_reads_exited_container_code_without_attached_client() -> None:
    executor = DockerNativeAgentExecutor(image="native-image")
    executor._docker = lambda _command: subprocess.CompletedProcess([], 0, "false 17\n", "")  # type: ignore[method-assign]

    assert executor._exited_container_code("native-test") == 17  # noqa: SLF001


def test_executor_environment_drops_control_plane_and_requires_github_grant() -> None:
    environment = {
        "PATH": "/usr/bin",
        "LANG": "C",
        "DATABASE_URL": "postgres://secret",
        "TEMPORAL_ADDRESS": "temporal:7233",
        "CODE_AGENT_API_SHARED_SECRET": "secret",
        "GH_TOKEN": "github-secret",
    }
    assert build_executor_environment(environment) == {"PATH": "/usr/bin", "LANG": "C"}
    granted = build_executor_environment(environment, allow_github_credentials=True)
    assert granted["GH_TOKEN"] == "github-secret"


def test_executor_agent_home_matches_native_command_scratch_namespace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert native_agent_home_for_request(workspace, "temporal-node-1") == node_agent_home(
        workspace, "temporal-node-1"
    )
    assert native_agent_home_for_request(workspace, None) == workspace / ".agent_home"
    assert sandbox_file_secret_dir_for_request(workspace, "temporal-node-1") == (
        node_scratch_root(workspace, "temporal-node-1") / "sandbox-secrets"
    )
    assert not sandbox_file_secret_dir_for_request(workspace, None).is_relative_to(workspace)


def test_provider_home_uses_explicit_source_not_ambiguous_environment() -> None:
    assert (
        provider_home_name(
            command=["agy", "-p", "prompt"], provider_auth_source=Path("/root/.gemini")
        )
        == ".gemini"
    )
    assert (
        provider_home_name(
            command=["agy", "-p", "prompt"], provider_auth_source=Path("/root/.codex")
        )
        == ".codex"
    )
    assert provider_home_name(command=["codex", "exec"], provider_auth_source=None) == ".codex"


def test_proxy_public_egress_rejects_private_and_dns_rebinding_addresses() -> None:
    assert is_public_egress_host("api.openai.com", ["104.18.33.45"])
    assert not is_public_egress_host("localhost", ["127.0.0.1"])
    assert not is_public_egress_host("metadata.google.internal", ["169.254.169.254"])
    assert not is_public_egress_host("carrier-grade-nat.example", ["100.64.0.1"])
    assert not is_public_egress_host("rebound.example", ["104.18.33.45", "10.0.0.8"])


def test_executor_auth_staging_failure_writes_startup_manifest(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "task"
    repo = workspace_root / "repo"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True)
    artifacts.mkdir()
    monkeypatch.setattr(
        "sandbox.provider_stager.ProviderCredentialStager.stage",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("staging denied")),
    )
    monkeypatch.setattr("os.chown", lambda path, uid, gid: None)
    monkeypatch.setattr("os.chmod", lambda path, mode: None)

    with pytest.raises(NativeAgentExecutorError, match="staging denied"):
        DockerNativeAgentExecutor(image="native-image").run(
            command=["agy", "-p", "prompt"],
            prompt="test prompt",
            workspace=native_executor_workspace_handle(
                workspace_path=workspace_root, repo_path=repo, task_id="task-1"
            ),
            artifact_root=artifacts,
            environment={},
            timeout_seconds=1,
            scratch_namespace="node-1",
            cancel_requested=None,
            redactor=None,
            context=TrustedSandboxExecutionContext(
                task_id="task-1",
                grant=SandboxCapabilityGrant(
                    network=NetworkEgressPolicy.DISABLED,
                    filesystem=FileSystemAccessPolicy.READ_ONLY,
                    allowed_secret_refs=frozenset(),
                ),
                secret_resolver=type("Resolver", (), {"resolve_for_sandbox": lambda *args: None})(),
                provider_bootstrap=ProviderBootstrap(
                    definitions=[], file_store={}, destination_by_ref={}, ref_names=tuple()
                ),
            ),
        )

    manifest = json.loads((artifacts / "native-isolation-manifest.json").read_text())
    assert manifest["termination_reason"] == "startup_error"
    assert manifest["cleanup"] == "removed"
    assert not native_agent_home_for_request(workspace_root, "node-1").exists()
    assert not sandbox_file_secret_dir_for_request(workspace_root, "node-1").exists()


def test_executor_cleans_network_when_polling_is_interrupted(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "task"
    repo = workspace_root / "repo"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True)
    artifacts.mkdir()
    executor = DockerNativeAgentExecutor(image="native-image")
    cleanup_calls: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr(
        "sandbox.provider_stager.ProviderCredentialStager.stage", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("os.chown", lambda path, uid, gid: None)
    monkeypatch.setattr("os.chmod", lambda path, mode: None)
    monkeypatch.setattr(executor, "_start_proxy", lambda **_kwargs: "native-egress-test")
    monkeypatch.setattr(
        executor,
        "_cleanup_network",
        lambda *, network_name, proxy_name: (
            cleanup_calls.append((network_name, proxy_name)) or "removed"
        ),
    )
    monkeypatch.setattr(
        "sandbox.native_agent_executor.subprocess.Popen",
        lambda *_args, **_kwargs: type(
            "Process",
            (),
            {"poll": lambda _self: None, "stdin": None, "stdout": None, "stderr": None},
        )(),
    )
    monkeypatch.setattr(
        "sandbox.native_agent_executor.time.sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        executor.run(
            command=["codex", "exec"],
            prompt="test prompt",
            workspace=native_executor_workspace_handle(
                workspace_path=workspace_root, repo_path=repo, task_id="task-1"
            ),
            artifact_root=artifacts,
            environment={},
            timeout_seconds=60,
            scratch_namespace="node-1",
            cancel_requested=None,
            redactor=None,
            context=TrustedSandboxExecutionContext(
                task_id="task-1",
                grant=SandboxCapabilityGrant(
                    network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
                    allowed_egress_hosts=("api.example.com",),
                    filesystem=FileSystemAccessPolicy.READ_ONLY,
                    allowed_secret_refs=frozenset(),
                ),
                secret_resolver=type("Resolver", (), {"resolve_for_sandbox": lambda *args: None})(),
                provider_bootstrap=ProviderBootstrap(
                    definitions=[], file_store={}, destination_by_ref={}, ref_names=tuple()
                ),
            ),
        )

    assert len(cleanup_calls) == 1
    network_name, proxy_name = cleanup_calls[0]
    assert network_name and network_name.startswith("native-agent-net-")
    assert proxy_name == "native-egress-test"
    assert not native_agent_home_for_request(workspace_root, "node-1").exists()
    assert not sandbox_file_secret_dir_for_request(workspace_root, "node-1").exists()


def test_proxy_audit_is_identity_only_and_redacts_request_content(
    tmp_path: Path, monkeypatch
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(native_agent_proxy, "_AUDIT_PATH", audit_path)
    monkeypatch.setenv("CODE_AGENT_PROXY_TASK_ID", "task-123")

    native_agent_proxy._audit(  # noqa: SLF001
        host="api.example.test", addresses=["104.18.33.45"], method="CONNECT", outcome="allowed"
    )

    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert payload == {
        "destination_host": "api.example.test",
        "destination_ips": ["104.18.33.45"],
        "method": "CONNECT",
        "outcome": "allowed",
        "task_id": "task-123",
        "timestamp": payload["timestamp"],
    }
    assert "authorization" not in audit_path.read_text(encoding="utf-8").lower()


@pytest.mark.asyncio
async def test_native_agent_runner_full_mocked_execution(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "task"
    repo = workspace_root / "repo"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True)
    artifacts.mkdir()
    tracked_file = repo / "tracked.py"
    tracked_file.write_text("value = 1\n", encoding="utf-8")
    git_dir = workspace_root / ".git"
    git_dir.mkdir()
    git_config = git_dir / "config"
    git_config.write_text("[core]\n", encoding="utf-8")
    executor = DockerNativeAgentExecutor(image="native-image")
    chowned_paths: list[Path] = []

    # Mock synchronous docker commands (like network creation/connection)
    monkeypatch.setattr(
        executor, "_docker", lambda command: subprocess.CompletedProcess(command, 0, "false 0", "")
    )

    # Mock proxy startup

    monkeypatch.setattr(executor, "_cleanup_network", lambda **kwargs: "removed")

    class MockProcess:
        def __init__(self, *args, **kwargs):
            self.returncode = 0
            self.args = args
            self.stdin = None

            class MockStream:
                def __init__(self):
                    self.closed = False

                def __iter__(self):
                    return iter(["output"])

                def read(self, *args):
                    if not self.closed:
                        self.closed = True
                        return "output"
                    return ""

            self.stdout = MockStream()
            self.stderr = MockStream()

        def poll(self):
            if not hasattr(self, "called"):
                self.called = True
                return None
            return 0

        def wait(self, *args, **kwargs):
            return 0

        def communicate(self, *args, **kwargs):
            return ("output", "")

        def kill(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(subprocess, "Popen", MockProcess)
    monkeypatch.setattr(executor, "_exited_container_code", lambda *args: 0)
    monkeypatch.setattr(
        "sandbox.native_agent_executor.os.chown",
        lambda path, _uid, _gid: chowned_paths.append(Path(path)),
    )
    from sandbox.capability import (
        CapabilityGrantFactory,
        FileSystemAccessPolicy,
        NetworkEgressPolicy,
    )
    from sandbox.secrets import InMemoryEphemeralSecretStore, SecretRegistry

    registry = SecretRegistry(ephemeral_store=InMemoryEphemeralSecretStore(), task_id="task-123")
    grant = CapabilityGrantFactory(registry).create_grant(
        network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
        filesystem=FileSystemAccessPolicy.WORKSPACE_WRITE,
        allowed_egress_hosts=["example.com"],
    )

    from sandbox.provider_bootstrap import ProviderBootstrap

    context = TrustedSandboxExecutionContext(
        grant=grant,
        task_id="task-123",
        provider_bootstrap=ProviderBootstrap(
            definitions=[], destination_by_ref={}, file_store={}, ref_names=()
        ),
        secret_resolver=registry,
    )

    from sandbox.secrets import ResolvedSecret, SecretResolver, SecretScope

    # Mock allowed_secret_refs and resolver
    object.__setattr__(
        context.grant, "allowed_secret_refs", ("env-sec", "mount-sec", "provider-sec")
    )

    def mock_resolve(handle, grant):
        if handle == "env-sec":
            return ResolvedSecret(
                name="env-sec",
                scope=list(SecretScope)[0],
                value="val",
                destination_env_var="ENV_SEC",
            )
        elif handle == "mount-sec":
            return ResolvedSecret(
                name="mount-sec",
                scope=list(SecretScope)[0],
                value="val",
                destination_mount_path="/run/secrets/code-agent/mount-sec",
            )
        elif handle == "provider-sec":
            return ResolvedSecret(
                name="provider-sec",
                scope=SecretScope.PROVIDER_AUTH,
                value="val",
                destination_env_var="PROV_SEC",
            )
        return ResolvedSecret(name=handle, scope=list(SecretScope)[0], value="val")

    context = TrustedSandboxExecutionContext(
        grant=context.grant,
        task_id=context.task_id,
        provider_bootstrap=context.provider_bootstrap,
        secret_resolver=SecretResolver(registry),
    )
    monkeypatch.setattr(context.secret_resolver, "resolve_for_sandbox", mock_resolve)

    result = executor.run(
        command=["echo", "test"],
        prompt="test prompt",
        workspace=native_executor_workspace_handle(
            workspace_path=workspace_root, repo_path=repo, task_id="task-123"
        ),
        artifact_root=artifacts,
        environment={},
        timeout_seconds=30,
        scratch_namespace="scratch-1",
        cancel_requested=lambda: False,
        redactor=None,
        context=context,
    )

    assert result.completed.returncode == 0
    assert workspace_root in chowned_paths
    assert repo in chowned_paths
    assert tracked_file in chowned_paths
    assert git_dir not in chowned_paths
    assert git_config not in chowned_paths
