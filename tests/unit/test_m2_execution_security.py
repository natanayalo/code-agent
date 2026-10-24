"""Task-private credential staging and legacy network/privilege regressions."""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sandbox.container import (
    DockerSandboxContainerManager,
    DockerSandboxContainerRequest,
    _build_docker_container_run_command,
)
from sandbox.legacy_egress import LegacyEgress
from sandbox.legacy_security import legacy_identity, prepare_legacy_workspace
from sandbox.native_agent_executor import DockerNativeAgentExecutor
from sandbox.provider_stager import ProviderCredentialStager, provider_credential_path
from sandbox.runner import DockerSandboxCommand, DockerSandboxRunner, _build_docker_run_command
from sandbox.secrets import ResolvedSecret, SecretScope
from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle


def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    return WorkspaceHandle(
        workspace_id="m2-synthetic",
        task_id="m2-synthetic",
        workspace_path=root,
        repo_path=root,
        repo_url="",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )


@pytest.mark.parametrize(
    "destination", ["../outside", "/outside", "x/../../outside", "x,y", "x\\y", "C:/x", "."]
)
def test_provider_destination_rejects_escape(tmp_path, destination):
    with pytest.raises(ValueError):
        provider_credential_path(tmp_path, destination)


@pytest.mark.parametrize("kind", ["parent", "leaf", "home", "hardlink", "directory"])
def test_provider_destination_rejects_aliases(tmp_path, kind):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "canary"
    target.write_text("unchanged")
    if kind == "home":
        home.rmdir()
        home.symlink_to(outside, target_is_directory=True)
    elif kind == "parent":
        (home / ".codex").symlink_to(outside, target_is_directory=True)
    else:
        (home / ".codex").mkdir()
        leaf = home / ".codex/auth.json"
        if kind == "leaf":
            leaf.symlink_to(target)
        elif kind == "hardlink":
            os.link(target, leaf)
        else:
            leaf.mkdir()
    with pytest.raises(ValueError):
        provider_credential_path(home, ".codex/auth.json")
    assert target.read_text() == "unchanged"


def test_stager_scopes_permissions_and_skips_unmapped_refs(tmp_path):
    auth = ResolvedSecret(name="auth", scope=SecretScope.PROVIDER_AUTH, value="synthetic")
    assert (
        ProviderCredentialStager.stage([auth], destination_by_ref={}, task_home=tmp_path)
        == tmp_path
    )
    ProviderCredentialStager.stage(
        [auth], destination_by_ref={"auth": ".codex/auth.json"}, task_home=tmp_path
    )
    path = tmp_path / ".codex/auth.json"
    assert path.read_text() == "synthetic"
    assert path.stat().st_mode & 0o777 == 0o600
    invalid = ResolvedSecret(name="auth", scope=SecretScope.GIT_PUSH, value="synthetic")
    with pytest.raises(ValueError, match="non-provider"):
        ProviderCredentialStager.stage([invalid], destination_by_ref={}, task_home=tmp_path)


def test_auth_mount_is_readonly_and_cannot_escape(tmp_path):
    handle = workspace(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    path = home / "auth.json"
    path.write_text("synthetic")
    kwargs = dict(
        container_name="synthetic",
        command=["true"],
        workspace=handle,
        artifact_root=tmp_path / "artifacts",
        agent_home=home,
        environment={},
        read_only_workspace=True,
        resource_limits=SimpleNamespace(pids_limit=128, memory_bytes=1024**3, cpu_limit=1),
    )
    executor = DockerNativeAgentExecutor()
    command = executor.build_run_command(**kwargs, provider_auth_paths=(path,))
    assert f"type=bind,source={path},target={path},readonly" in command
    with pytest.raises(RuntimeError, match="escapes"):
        executor.build_run_command(**kwargs, provider_auth_paths=(tmp_path / "outside",))


def test_root_and_windows_brokers_never_produce_root_children(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0)
    assert legacy_identity() == (65532, 65532)
    monkeypatch.delattr(os, "getuid")
    monkeypatch.delattr(os, "getgid")
    assert legacy_identity() == (65532, 65532)


def test_root_prepares_only_task_writable_paths(tmp_path, monkeypatch):
    handle = workspace(tmp_path)
    (handle.workspace_path / "file").touch()
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: calls.append(args))
    prepare_legacy_workspace(handle.workspace_path, read_only=True)
    assert not calls
    prepare_legacy_workspace(handle.workspace_path, read_only=False)
    assert calls
    assert all(str(call[0]).startswith(str(handle.workspace_path)) for call in calls)


def test_networked_legacy_refuses_arbitrary_credentials_before_docker(tmp_path, monkeypatch):
    start = MagicMock()
    monkeypatch.setattr(DockerNativeAgentExecutor, "_start_proxy", start)
    with pytest.raises(ValueError, match="unscoped"):
        LegacyEgress.start(
            environment={"API_TOKEN": "synthetic"},
            artifact_root=tmp_path,
            task_id="m2",
            docker_binary="docker",
        )
    start.assert_not_called()


def test_legacy_proxy_start_and_cleanup_use_owned_names(tmp_path, monkeypatch):
    start, cleanup = MagicMock(return_value="owned-proxy"), MagicMock(return_value="removed")
    monkeypatch.setattr(DockerNativeAgentExecutor, "_start_proxy", start)
    monkeypatch.setattr(DockerNativeAgentExecutor, "_cleanup_network", cleanup)
    egress = LegacyEgress.start(
        environment={}, artifact_root=tmp_path, task_id="m2", docker_binary="docker"
    )
    assert egress.network.startswith("legacy-agent-net-")
    assert start.call_args.kwargs["network_policy"] == "public_https_proxy"
    egress.close()
    cleanup.assert_called_once_with(network_name=egress.network, proxy_name="owned-proxy")


@pytest.mark.parametrize("failure", [False, True])
def test_networked_container_cleans_proxy_on_failure_or_stop(tmp_path, monkeypatch, failure):
    handle = workspace(tmp_path)
    egress = SimpleNamespace(network="owned-net", proxy="owned-proxy", close=MagicMock())
    monkeypatch.setattr(LegacyEgress, "start", lambda **kwargs: egress)
    runner = MagicMock(return_value=subprocess.CompletedProcess([], 1 if failure else 0, "", ""))
    manager = DockerSandboxContainerManager(command_runner=runner)
    request = DockerSandboxContainerRequest(workspace=handle, network_enabled=True)
    if failure:
        with pytest.raises(RuntimeError):
            manager.start(request)
        egress.close.assert_called_once()
    else:
        container = manager.start(request)
        command = runner.call_args.args[0]
        assert command[command.index("--network") + 1] == "owned-net"
        assert "HTTP_PROXY=http://native-egress-proxy:8080" in command
        cleanup = MagicMock(return_value="removed")
        monkeypatch.setattr(DockerNativeAgentExecutor, "_cleanup_network", cleanup)
        manager.stop(container)
        cleanup.assert_called_once_with(network_name="owned-net", proxy_name="owned-proxy")


def test_oneshot_cleanup_runs_when_execution_fails(tmp_path, monkeypatch):
    egress = SimpleNamespace(network="owned-net", proxy="owned-proxy", close=MagicMock())
    monkeypatch.setattr(LegacyEgress, "start", lambda **kwargs: egress)
    runner = DockerSandboxRunner(command_runner=MagicMock(side_effect=OSError("synthetic")))
    with pytest.raises(OSError):
        runner.run(
            DockerSandboxCommand(
                workspace=workspace(tmp_path), command=["true"], network_enabled=True
            )
        )
    egress.close.assert_called_once()


@pytest.mark.parametrize("phase", ["create", "run", "attach"])
@pytest.mark.parametrize("exception", [False, True])
def test_proxy_failure_never_leaves_owned_network_untracked(
    tmp_path, monkeypatch, phase, exception
):
    executor = DockerNativeAgentExecutor()
    cleanup = MagicMock(return_value="removed")

    def command(args):
        current = "run" if args[0] == "run" else ("create" if args[1] == "create" else "attach")
        if current == phase:
            if exception:
                raise OSError("synthetic failure")
            return subprocess.CompletedProcess([], 1, "", "synthetic failure")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(executor, "_docker", command)
    monkeypatch.setattr(executor, "_cleanup_network", cleanup)
    with pytest.raises((OSError, RuntimeError)):
        executor._start_proxy(
            network_name="owned-network",
            artifact_root=tmp_path,
            task_id="m2",
            network_policy="public_https_proxy",
            allowed_hosts=[],
        )
    if phase == "create" and not exception:
        cleanup.assert_not_called()  # The daemon refused creation; nothing was allocated.
    else:
        assert cleanup.call_args.kwargs["network_name"] == "owned-network"
        proxy = cleanup.call_args.kwargs["proxy_name"]
        assert proxy is None if phase == "create" else proxy.startswith("native-egress-")


def test_failed_legacy_cleanup_is_visible(caplog):
    executor = MagicMock()
    executor._cleanup_network.return_value = "cleanup_failed:network"
    LegacyEgress(executor, "owned-network", "owned-proxy").close()
    assert "Incomplete legacy egress cleanup" in caplog.text


def test_root_broker_rejects_symlinked_writable_workspace(tmp_path, monkeypatch):
    handle = workspace(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(handle.workspace_path, target_is_directory=True)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(ValueError, match="symlinked"):
        prepare_legacy_workspace(alias, read_only=False)


def test_network_cannot_bypass_proxy_or_secret_audience_validation(tmp_path):
    handle = workspace(tmp_path)
    with pytest.raises(RuntimeError, match="task-private"):
        _build_docker_container_run_command(
            DockerSandboxContainerRequest(workspace=handle, network_enabled=True), image="test"
        )
    request = DockerSandboxCommand(workspace=handle, command=["true"], network_enabled=True)
    with pytest.raises(RuntimeError, match="task-private"):
        _build_docker_run_command(request, image="test")
    with pytest.raises(RuntimeError, match="native grants"):
        DockerSandboxRunner().run(request.model_copy(update={"secrets": {"key": "synthetic"}}))


def test_stop_failure_still_closes_legacy_egress(tmp_path, monkeypatch):
    manager = DockerSandboxContainerManager(
        command_runner=MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    )
    container = manager.start(DockerSandboxContainerRequest(workspace=workspace(tmp_path)))
    container.egress_network, container.egress_proxy = "owned-network", "owned-proxy"
    cleanup = MagicMock(return_value="removed")
    monkeypatch.setattr(DockerNativeAgentExecutor, "_cleanup_network", cleanup)
    manager._command_runner = MagicMock(side_effect=OSError("synthetic failure"))
    with pytest.raises(OSError):
        manager.stop(container)
    cleanup.assert_called_once_with(network_name="owned-network", proxy_name="owned-proxy")
