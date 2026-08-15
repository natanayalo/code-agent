"""Security regression tests for Docker-native agent isolation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import sandbox.native_agent_proxy as native_agent_proxy
from sandbox.native_agent_executor import (
    DockerNativeAgentExecutor,
    NativeAgentExecutorError,
    build_executor_environment,
    is_public_egress_host,
    native_agent_home_for_request,
    native_executor_workspace_handle,
    provider_home_name,
    stage_provider_auth,
)
from sandbox.scratch import node_agent_home


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


def test_stage_provider_auth_copies_selected_files_not_the_source_home(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
    (source / "oauth_creds.json").write_text('{"refresh_token":"secret"}', encoding="utf-8")
    token_path = source / "antigravity-cli" / "antigravity-oauth-token"
    token_path.parent.mkdir()
    token_path.write_text("secret", encoding="utf-8")
    (source / "antigravity-cli" / "history.jsonl").write_text("do not copy", encoding="utf-8")
    config_path = source / "config" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text("{}", encoding="utf-8")
    (source / "unrelated-secret.txt").write_text("do not copy", encoding="utf-8")

    destination_token = destination / "antigravity-cli" / "antigravity-oauth-token"
    destination_token.parent.mkdir(parents=True)
    destination_token.symlink_to(token_path)

    stage_provider_auth(source=source, destination=destination)

    assert (destination / "auth.json").read_text(encoding="utf-8") == '{"token":"secret"}'
    assert (destination / "oauth_creds.json").is_file()
    assert destination_token.is_file()
    assert not destination_token.is_symlink()
    assert destination_token.read_text(encoding="utf-8") == "secret"
    assert (destination / "config" / "config.json").is_file()
    assert not (destination / "antigravity-cli" / "history.jsonl").exists()
    assert not (destination / "unrelated-secret.txt").exists()


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
        "sandbox.native_agent_executor.stage_provider_auth",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("staging denied")),
    )

    with pytest.raises(NativeAgentExecutorError, match="staging denied"):
        DockerNativeAgentExecutor(image="native-image").run(
            command=["agy", "-p", "prompt"],
            prompt=None,
            workspace=native_executor_workspace_handle(
                workspace_path=workspace_root, repo_path=repo, task_id="task-1"
            ),
            artifact_root=artifacts,
            environment={},
            timeout_seconds=1,
            read_only_workspace=False,
            scratch_namespace="node-1",
            cancel_requested=None,
            redactor=None,
            network_enabled=False,
            github_credentials={},
            provider_auth_source=tmp_path / ".gemini",
        )

    manifest = json.loads((artifacts / "native-isolation-manifest.json").read_text())
    assert manifest["termination_reason"] == "startup_error"
    assert manifest["cleanup"] == "removed"


def test_executor_cleans_network_when_polling_is_interrupted(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "task"
    repo = workspace_root / "repo"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True)
    artifacts.mkdir()
    executor = DockerNativeAgentExecutor(image="native-image")
    cleanup_calls: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr("sandbox.native_agent_executor.stage_provider_auth", lambda **_kwargs: None)
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
            "Process", (), {"poll": lambda _self: None, "stdin": None}
        )(),
    )
    monkeypatch.setattr(
        "sandbox.native_agent_executor.time.sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        executor.run(
            command=["codex", "exec"],
            prompt=None,
            workspace=native_executor_workspace_handle(
                workspace_path=workspace_root, repo_path=repo, task_id="task-1"
            ),
            artifact_root=artifacts,
            environment={},
            timeout_seconds=60,
            read_only_workspace=True,
            scratch_namespace="node-1",
            cancel_requested=None,
            redactor=None,
            network_enabled=True,
            github_credentials={},
            provider_auth_source=tmp_path / ".codex",
        )

    assert len(cleanup_calls) == 1
    network_name, proxy_name = cleanup_calls[0]
    assert network_name and network_name.startswith("native-agent-net-")
    assert proxy_name == "native-egress-test"


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
