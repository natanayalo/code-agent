from pathlib import Path
from unittest.mock import MagicMock

from apps.api.task_service_factory import (
    LEGACY_NATIVE_AGENT_EVENT_CAPTURE_ENABLED_ENV_VAR,
    NATIVE_AGENT_EVENT_CAPTURE_ENABLED_ENV_VAR,
    _build_codex_worker,
    _build_gemini_worker,
)
from db.enums import WorkerRuntimeMode
from sandbox import WorkspaceCleanupPolicy, WorkspaceHandle
from tools import DEFAULT_TOOL_REGISTRY
from workers.antigravity_cli_adapter import (
    ANTIGRAVITY_EXECUTABLE_ENV_VAR,
    AntigravityCliRuntimeAdapter,
)
from workers.antigravity_event_normalizer import AntigravityStreamNormalizer
from workers.base import WorkerRequest
from workers.cli_runtime import CliRuntimeSettings
from workers.codex_cli_worker import CodexCliWorker
from workers.codex_event_normalizer import CodexStreamNormalizer
from workers.gemini_cli_worker import GeminiCliWorker


def _make_dummy_workspace(tmp_path: Path) -> WorkspaceHandle:
    ws_path = tmp_path / "ws"
    repo_path = ws_path / "repo"
    repo_path.mkdir(parents=True)
    return WorkspaceHandle(
        workspace_id="test_ws",
        task_id="task_1",
        workspace_path=ws_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        branch="main",
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )


def _setup_antigravity_auth(tmp_path: Path) -> AntigravityCliRuntimeAdapter:
    provider_home = tmp_path / "provider-home"
    token_path = provider_home / "antigravity-cli" / "antigravity-oauth-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text('{"token":{"access_token":"test-access","refresh_token":"test-refresh"}}')
    return AntigravityCliRuntimeAdapter(
        executable="/opt/bin/agy",
        model="gemini-3-pro",
        env={"GEMINI_HOME": str(provider_home)},
    )


def test_codex_worker_flag_wiring_enabled(tmp_path: Path):
    workspace = _make_dummy_workspace(tmp_path)
    worker = CodexCliWorker(
        runtime_adapter=MagicMock(),
        tool_registry=DEFAULT_TOOL_REGISTRY,
        native_event_capture_enabled=True,
    )
    request = WorkerRequest(task_text="Run test", scratch_namespace="test_ns")
    run_req, _ = worker._prepare_native_agent_run_request(
        request,
        workspace,
        CliRuntimeSettings(),
        WorkerRuntimeMode.NATIVE_AGENT,
        "system prompt",
    )
    assert isinstance(run_req.normalizer, CodexStreamNormalizer)
    assert run_req.worker_type == "codex"


def test_codex_worker_flag_wiring_disabled(tmp_path: Path):
    workspace = _make_dummy_workspace(tmp_path)
    worker = CodexCliWorker(
        runtime_adapter=MagicMock(),
        tool_registry=DEFAULT_TOOL_REGISTRY,
        native_event_capture_enabled=False,
    )
    request = WorkerRequest(task_text="Run test", scratch_namespace="test_ns")
    run_req, _ = worker._prepare_native_agent_run_request(
        request,
        workspace,
        CliRuntimeSettings(),
        WorkerRuntimeMode.NATIVE_AGENT,
        "system prompt",
    )
    assert run_req.normalizer is None
    assert run_req.worker_type == "codex"


def test_gemini_worker_flag_wiring_enabled(tmp_path: Path):
    workspace = _make_dummy_workspace(tmp_path)
    adapter = _setup_antigravity_auth(tmp_path)
    worker = GeminiCliWorker(
        runtime_adapter=adapter,
        tool_registry=DEFAULT_TOOL_REGISTRY,
        native_event_capture_enabled=True,
    )
    request = WorkerRequest(task_text="Run test", scratch_namespace="test_ns")
    run_req, _ = worker._build_native_agent_run_request(
        request,
        workspace=workspace,
        runtime_settings=CliRuntimeSettings(),
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        system_prompt_override="system prompt",
    )
    assert isinstance(run_req.normalizer, AntigravityStreamNormalizer)
    assert run_req.worker_type == "antigravity"
    assert "--output-format" in run_req.command
    assert "stream-json" in run_req.command


def test_gemini_worker_flag_wiring_disabled(tmp_path: Path):
    workspace = _make_dummy_workspace(tmp_path)
    adapter = _setup_antigravity_auth(tmp_path)
    worker = GeminiCliWorker(
        runtime_adapter=adapter,
        tool_registry=DEFAULT_TOOL_REGISTRY,
        native_event_capture_enabled=False,
    )
    request = WorkerRequest(task_text="Run test", scratch_namespace="test_ns")
    run_req, _ = worker._build_native_agent_run_request(
        request,
        workspace=workspace,
        runtime_settings=CliRuntimeSettings(),
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        system_prompt_override="system prompt",
    )
    assert run_req.normalizer is None
    assert run_req.worker_type == "antigravity"
    assert "--output-format" not in run_req.command


def test_task_service_factory_env_propagation():
    assert NATIVE_AGENT_EVENT_CAPTURE_ENABLED_ENV_VAR == "CODE_AGENT_NATIVE_EVENT_CAPTURE_ENABLED"
    assert (
        LEGACY_NATIVE_AGENT_EVENT_CAPTURE_ENABLED_ENV_VAR
        == "CODE_AGENT_NATIVE_AGENT_EVENT_CAPTURE_ENABLED"
    )
    cm = MagicMock()
    store = MagicMock()

    # Canonical key
    env_on = {
        NATIVE_AGENT_EVENT_CAPTURE_ENABLED_ENV_VAR: "1",
        ANTIGRAVITY_EXECUTABLE_ENV_VAR: "/usr/local/bin/antigravity",
    }
    codex_on = _build_codex_worker(env_on, cm, store)
    assert codex_on.native_event_capture_enabled is True
    gemini_on = _build_gemini_worker(env_on, cm, store)
    assert gemini_on is not None
    assert gemini_on.native_event_capture_enabled is True

    # Legacy key fallback during migration
    env_legacy_on = {
        LEGACY_NATIVE_AGENT_EVENT_CAPTURE_ENABLED_ENV_VAR: "1",
        ANTIGRAVITY_EXECUTABLE_ENV_VAR: "/usr/local/bin/antigravity",
    }
    codex_legacy = _build_codex_worker(env_legacy_on, cm, store)
    assert codex_legacy.native_event_capture_enabled is True
    gemini_legacy = _build_gemini_worker(env_legacy_on, cm, store)
    assert gemini_legacy is not None
    assert gemini_legacy.native_event_capture_enabled is True

    # Precedence: canonical takes precedence over legacy
    env_precedence = {
        NATIVE_AGENT_EVENT_CAPTURE_ENABLED_ENV_VAR: "0",
        LEGACY_NATIVE_AGENT_EVENT_CAPTURE_ENABLED_ENV_VAR: "1",
        ANTIGRAVITY_EXECUTABLE_ENV_VAR: "/usr/local/bin/antigravity",
    }
    codex_prec = _build_codex_worker(env_precedence, cm, store)
    assert codex_prec.native_event_capture_enabled is False

    env_off = {
        NATIVE_AGENT_EVENT_CAPTURE_ENABLED_ENV_VAR: "0",
        ANTIGRAVITY_EXECUTABLE_ENV_VAR: "/usr/local/bin/antigravity",
    }
    codex_off = _build_codex_worker(env_off, cm, store)
    assert codex_off.native_event_capture_enabled is False
    gemini_off = _build_gemini_worker(env_off, cm, store)
    assert gemini_off is not None
    assert gemini_off.native_event_capture_enabled is False
