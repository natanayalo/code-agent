"""A task-private proxy must listen before a legacy or native task starts."""

import subprocess
from unittest.mock import MagicMock

import pytest

from sandbox.native_agent_executor import DockerNativeAgentExecutor, NativeAgentExecutorError


def test_proxy_readiness_checks_only_owned_container(monkeypatch):
    executor = DockerNativeAgentExecutor()
    command = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(executor, "_docker", command)
    executor._wait_for_proxy("owned-proxy")
    args = command.call_args.args[0]
    assert args[:4] == ["exec", "owned-proxy", "python", "-c"]
    assert "127.0.0.1" in args[-1] and "monotonic()+10" in args[-1]


def test_proxy_readiness_failure_cleans_owned_resources(tmp_path, monkeypatch):
    executor = DockerNativeAgentExecutor()
    cleanup = MagicMock(return_value="removed")
    monkeypatch.setattr(executor, "_cleanup_network", cleanup)
    monkeypatch.setattr(
        executor,
        "_docker",
        lambda args: subprocess.CompletedProcess(args, 1 if args[0] == "exec" else 0),
    )
    with pytest.raises(NativeAgentExecutorError, match="listener"):
        executor._start_proxy(
            network_name="owned-net",
            artifact_root=tmp_path,
            task_id="owned-task",
            network_policy="public_https_proxy",
            allowed_hosts=[],
        )
    cleanup.assert_called_once()
    assert cleanup.call_args.kwargs["network_name"] == "owned-net"
