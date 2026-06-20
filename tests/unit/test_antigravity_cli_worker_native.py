"""Unit tests for Antigravity-backed native worker execution."""

from __future__ import annotations

import json
from pathlib import Path

from db.enums import WorkerRuntimeMode
from sandbox import WorkspaceCleanupPolicy, WorkspaceHandle
from workers import AntigravityCliRuntimeAdapter, GeminiCliWorker, WorkerRequest
from workers.antigravity_cli_worker_native import (
    AntigravityCommandConfig,
    build_antigravity_native_command,
)
from workers.base import WorkerResult
from workers.cli_runtime import CliRuntimeSettings
from workers.native_agent_runner import NativeAgentRunResult


class _FakeWorkspaceManager:
    def __init__(self, workspace: WorkspaceHandle) -> None:
        self.workspace = workspace
        self.cleanup_requests: list[tuple[WorkspaceHandle, bool]] = []

    def create_workspace(self, request: object) -> WorkspaceHandle:
        return self.workspace

    def cleanup_workspace(self, workspace: WorkspaceHandle, *, succeeded: bool) -> bool:
        self.cleanup_requests.append((workspace, succeeded))
        return False


class _FakeContainerManager:
    def stop(self, container: object) -> None:
        raise AssertionError("native Antigravity run should not start a tool-loop container")


def _make_workspace(tmp_path: Path) -> WorkspaceHandle:
    repo_path = tmp_path / "repo"
    repo_path.mkdir(parents=True)
    return WorkspaceHandle(
        workspace_id="ws-antigravity-test",
        task_id="antigravity-test-task",
        workspace_path=tmp_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        branch="main",
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )


def _make_worker(
    tmp_path: Path,
    *,
    tool_permission: str = "proceed-in-sandbox",
    native_sandbox_enabled: bool = True,
) -> tuple[GeminiCliWorker, WorkspaceHandle]:
    workspace = _make_workspace(tmp_path)
    worker = GeminiCliWorker(
        runtime_adapter=AntigravityCliRuntimeAdapter(
            executable="/opt/bin/agy",
            model="gemini-3-pro",
            tool_permission=tool_permission,
            artifact_review_policy="auto",
        ),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(),  # type: ignore[arg-type]
        native_sandbox_enabled=native_sandbox_enabled,
    )
    return worker, workspace


def test_antigravity_worker_builds_prompt_argv_command_and_settings(tmp_path: Path) -> None:
    worker, workspace = _make_worker(tmp_path)
    native_request, provider_metadata = worker._build_native_agent_run_request(
        WorkerRequest(
            session_id="session-antigravity",
            repo_url="https://example.com/repo.git",
            branch="main",
            task_text="Apply a small Antigravity change",
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        ),
        workspace=workspace,
        runtime_settings=CliRuntimeSettings(),
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        system_prompt_override="system prompt",
    )
    command = native_request.command
    assert command[:3] == ["/opt/bin/agy", "-p", native_request.prompt]
    assert "--cwd" in command
    assert command[command.index("--cwd") + 1] == str(workspace.repo_path)
    assert "--model" in command
    assert command[command.index("--model") + 1] == "gemini-3-pro"
    assert native_request.stdin_prompt is False
    assert native_request.command_redactions == [native_request.prompt]
    assert native_request.env is not None
    assert native_request.env["GEMINI_HOME"] == str(
        workspace.workspace_path / ".agent_home" / ".gemini"
    )
    assert (
        native_request.events_path
        == workspace.workspace_path / ".code-agent" / "antigravity-native.log"
    )

    settings_path = (
        workspace.workspace_path / ".agent_home" / ".gemini" / "antigravity-cli" / "settings.json"
    )
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings == {
        "artifactReviewPolicy": "agent-decides",
        "enableTerminalSandbox": True,
        "toolPermission": "proceed-in-sandbox",
    }
    assert provider_metadata["provider"] == "antigravity"
    assert provider_metadata["tool_permission"] == "proceed-in-sandbox"
    assert provider_metadata["gemini_home"] == str(
        workspace.workspace_path / ".agent_home" / ".gemini"
    )


def test_antigravity_workspace_migration_replaces_symlink_and_copies_legacy_config(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path / "workspace")
    legacy_gemini_home = tmp_path / "legacy-gemini"
    legacy_gemini_home.mkdir()
    (legacy_gemini_home / "GEMINI.md").write_text("global rules\n", encoding="utf-8")
    (legacy_gemini_home / "skills" / "global-skill").mkdir(parents=True)
    (legacy_gemini_home / "skills" / "global-skill" / "SKILL.md").write_text(
        "---\nname: global\n---\n",
        encoding="utf-8",
    )
    (legacy_gemini_home / "settings.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://example.com/sse",
                        "httpUrl": "https://example.com/unused",
                        "header": "kept",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    legacy_workspace_skills = workspace.repo_path / ".gemini" / "skills" / "local-skill"
    legacy_workspace_skills.mkdir(parents=True)
    (legacy_workspace_skills / "SKILL.md").write_text("---\nname: local\n---\n", encoding="utf-8")
    (workspace.repo_path / ".gemini" / "settings.json").write_text(
        json.dumps({"mcpServers": {"local": {"httpUrl": "https://example.com/ws"}}}),
        encoding="utf-8",
    )
    agent_home = workspace.workspace_path / ".agent_home"
    agent_home.mkdir()
    (agent_home / ".gemini").symlink_to(legacy_gemini_home, target_is_directory=True)

    _, _, metadata = build_antigravity_native_command(
        AntigravityCommandConfig(
            adapter=AntigravityCliRuntimeAdapter(executable="/opt/bin/agy"),
            workspace=workspace,
            request=WorkerRequest(repo_url="https://example.com/repo.git", task_text="run"),
            prompt="run",
            runtime_settings=CliRuntimeSettings(),
            native_sandbox_enabled=True,
        )
    )

    gemini_home = agent_home / ".gemini"
    assert not gemini_home.is_symlink()
    assert (gemini_home / "GEMINI.md").read_text(encoding="utf-8") == "global rules\n"
    assert (gemini_home / "antigravity-cli" / "skills" / "global-skill" / "SKILL.md").exists()
    assert json.loads((gemini_home / "config" / "mcp_config.json").read_text()) == {
        "mcpServers": {"remote": {"header": "kept", "serverUrl": "https://example.com/sse"}}
    }
    assert (workspace.repo_path / ".agents" / "skills" / "local-skill" / "SKILL.md").exists()
    assert json.loads((workspace.repo_path / ".agents" / "mcp_config.json").read_text()) == {
        "mcpServers": {"local": {"serverUrl": "https://example.com/ws"}}
    }
    assert not (legacy_gemini_home / "antigravity-cli" / "settings.json").exists()
    assert "replaced_symlinked_gemini_home" in metadata["migration_actions"]


def test_antigravity_workspace_migration_copy_errors_are_best_effort(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _make_workspace(tmp_path / "workspace")
    legacy_gemini_home = tmp_path / "legacy-gemini"
    (legacy_gemini_home / "skills" / "global-skill").mkdir(parents=True)
    agent_home = workspace.workspace_path / ".agent_home"
    agent_home.mkdir()
    (agent_home / ".gemini").symlink_to(legacy_gemini_home, target_is_directory=True)

    def _raise_copy_error(*_args, **_kwargs):
        raise OSError("blocked legacy skills")

    monkeypatch.setattr(
        "workers.antigravity_cli_worker_native.shutil.copytree",
        _raise_copy_error,
    )

    command, _, metadata = build_antigravity_native_command(
        AntigravityCommandConfig(
            adapter=AntigravityCliRuntimeAdapter(executable="/opt/bin/agy"),
            workspace=workspace,
            request=WorkerRequest(repo_url="https://example.com/repo.git", task_text="run"),
            prompt="run",
            runtime_settings=CliRuntimeSettings(),
            native_sandbox_enabled=True,
        )
    )

    assert command[:2] == ["/opt/bin/agy", "-p"]
    assert "copied_global_skills" not in metadata["migration_actions"]
    assert "replaced_symlinked_gemini_home" in metadata["migration_actions"]


def test_antigravity_workspace_migration_skips_inaccessible_candidate_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _make_workspace(tmp_path / "workspace")
    blocked_home = Path("/root/.gemini")

    def _candidate_gemini_homes(_adapter, _symlink_source):
        yield blocked_home

    original_exists = Path.exists

    def _exists(path: Path, *args: object, **kwargs: object) -> bool:
        if path == blocked_home:
            raise PermissionError("blocked root gemini home")
        return original_exists(path, *args, **kwargs)

    monkeypatch.setattr(
        "workers.antigravity_cli_worker_native._candidate_gemini_homes",
        _candidate_gemini_homes,
    )
    monkeypatch.setattr(Path, "exists", _exists)

    command, _, metadata = build_antigravity_native_command(
        AntigravityCommandConfig(
            adapter=AntigravityCliRuntimeAdapter(executable="/opt/bin/agy"),
            workspace=workspace,
            request=WorkerRequest(repo_url="https://example.com/repo.git", task_text="run"),
            prompt="run",
            runtime_settings=CliRuntimeSettings(),
            native_sandbox_enabled=True,
        )
    )

    assert command[:2] == ["/opt/bin/agy", "-p"]
    assert metadata["migration_actions"] == []
    assert (workspace.workspace_path / ".agent_home" / ".gemini").is_dir()


def test_antigravity_worker_read_only_uses_strict_tool_permission(tmp_path: Path) -> None:
    worker, workspace = _make_worker(tmp_path, tool_permission="always-proceed")

    worker._build_native_agent_run_request(
        WorkerRequest(
            repo_url="https://example.com/repo.git",
            task_text="Inspect only",
            constraints={"read_only": True},
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        ),
        workspace=workspace,
        runtime_settings=CliRuntimeSettings(),
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        system_prompt_override="system prompt",
    )

    settings_path = (
        workspace.workspace_path / ".agent_home" / ".gemini" / "antigravity-cli" / "settings.json"
    )
    assert json.loads(settings_path.read_text(encoding="utf-8"))["toolPermission"] == "strict"


def test_antigravity_worker_maps_auth_failure_to_provider_auth(tmp_path: Path) -> None:
    worker, _ = _make_worker(tmp_path)
    result = worker._build_worker_result_from_native_run(
        _make_workspace(tmp_path / "manual"),
        NativeAgentRunResult(
            status="failure",
            summary="Native agent command exited with code 2.",
            command="agy --print [REDACTED]",
            exit_code=2,
            duration_seconds=0.1,
            timed_out=False,
            stderr="authentication failed: keyring is locked",
        ),
        WorkerRuntimeMode.NATIVE_AGENT,
        None,
        {"provider": "antigravity"},
    )

    assert isinstance(result, WorkerResult)
    assert result.failure_kind == "provider_auth"


def test_antigravity_worker_maps_permission_denial_to_permission_denied(tmp_path: Path) -> None:
    worker, _ = _make_worker(tmp_path)
    result = worker._build_worker_result_from_native_run(
        _make_workspace(tmp_path / "manual"),
        NativeAgentRunResult(
            status="failure",
            summary="Native agent command exited with code 3.",
            command="agy --print [REDACTED]",
            exit_code=3,
            duration_seconds=0.1,
            timed_out=False,
            stderr="permission denied by tool permission policy",
        ),
        WorkerRuntimeMode.NATIVE_AGENT,
        None,
        {"provider": "antigravity"},
    )

    assert result.failure_kind == "permission_denied"
    assert result.next_action_hint == "request_higher_permission"
