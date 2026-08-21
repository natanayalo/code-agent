"""Unit tests to increase coverage of gemini_cli_worker_native."""

from pathlib import Path
from unittest.mock import MagicMock

from sandbox.secrets import SecretExposurePolicy, SecretRef
from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle
from workers.base import WorkerRequest, WorkerRuntimeMode
from workers.cli_runtime_types import CliRuntimeSettings
from workers.gemini_cli_worker_native import GeminiCliWorkerNativeMixin
from workers.native_agent_runner import NativeAgentRunRequest


def test_prepare_workspace_gemini_home_oserror_fallback(monkeypatch, tmp_path):
    class FakeGeminiWorker(GeminiCliWorkerNativeMixin):
        def __init__(self):
            pass

    worker = FakeGeminiWorker()
    worker.tool_registry = MagicMock()
    worker.runtime_adapter = MagicMock()
    worker.native_event_capture_enabled = False
    worker.build_memory_context_string = lambda *args, **kwargs: ""
    worker.is_native_mode = lambda *args, **kwargs: True
    worker._build_native_command = lambda *args, **kwargs: ["echo"]
    worker._build_native_prompt = lambda *args, **kwargs: ""

    request = NativeAgentRunRequest(
        command=["echo"], prompt="echo", repo_path=tmp_path / "repo", workspace_path=tmp_path / "ws"
    )
    request.workspace_path.mkdir(parents=True)

    def fake_exists(self):
        if str(self) == "/root/.gemini":
            return True
        raise OSError("denied")

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "expanduser", lambda x: x)

    from workers.gemini_cli_worker_native import _prepare_workspace_gemini_home

    # Should safely swallow OSError and not crash
    _prepare_workspace_gemini_home(workspace_path=request.workspace_path)


def test_gemini_native_prepare_request_secret_refs(monkeypatch, tmp_path):
    class FakeGeminiWorker(GeminiCliWorkerNativeMixin):
        def __init__(self):
            pass

    worker = FakeGeminiWorker()
    worker.tool_registry = MagicMock()
    worker.runtime_adapter = MagicMock()
    worker.native_event_capture_enabled = False
    worker.build_memory_context_string = lambda *args, **kwargs: ""
    worker.is_native_mode = lambda *args, **kwargs: True
    worker._build_native_command = lambda *args, **kwargs: ["echo"]
    worker._build_native_prompt = lambda *args, **kwargs: ""

    request = WorkerRequest(
        task_text="echo",
        repo_url="https://github.com/foo/bar",
        secret_refs=[SecretRef(name="my_secret")],
    )

    workspace = WorkspaceHandle(
        workspace_path=tmp_path / "ws",
        repo_path=tmp_path / "repo",
        workspace_id="123",
        task_id="abc",
        repo_url="https://github.com/foo/bar",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )

    runtime_settings = CliRuntimeSettings()

    # Mock registry and bootstrap
    class FakeDefinition:
        name = "builtin"
        exposure_policy = SecretExposurePolicy.SANDBOX_ENV
        source_key = "opaque-gemini-key-handle"
        destination_env_var = "GEMINI_API_KEY"

    class FakeBootstrap:
        definitions = [FakeDefinition()]
        file_store = None

    class FakeRegistry:
        def __init__(self, *args, **kwargs):
            pass

        def register(self, *args, **kwargs):
            pass

        def get(self, name, **kwargs):
            if name == "my_secret":
                return FakeDefinition()
            return None

    class FakeResolved:
        def reveal_secret_value(self):
            return "secret_val"

    class FakeResolver:
        def __init__(self, *args, **kwargs):
            pass

        def resolve_for_sandbox(self, *args, **kwargs):
            return FakeResolved()

    class FakeContext:
        def __init__(self, *args, **kwargs):
            pass

    class FakeGrant:
        def __init__(self, *args, **kwargs):
            pass

        def create_grant(self, *args, **kwargs):
            return FakeGrant()

    load_calls: list[bool] = []

    def _load_bootstrap(*_args, **kwargs):
        load_calls.append(kwargs["has_api_key"])
        return FakeBootstrap()

    monkeypatch.setattr(
        "sandbox.provider_bootstrap.ProviderBootstrapLoader.load",
        _load_bootstrap,
    )
    monkeypatch.setattr("sandbox.secrets.SecretRegistry", FakeRegistry)
    monkeypatch.setattr("sandbox.capability.CapabilityGrantFactory", FakeGrant)
    monkeypatch.setattr("sandbox.secrets.SecretResolver", FakeResolver)
    monkeypatch.setattr("sandbox.trusted_context.TrustedSandboxExecutionContext", FakeContext)
    monkeypatch.setattr(
        "sandbox.capability.validate_grant_for_execution", lambda *args, **kwargs: None
    )

    result, md = worker._build_native_agent_run_request(
        request,
        workspace=workspace,
        runtime_settings=runtime_settings,
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        system_prompt_override=None,
    )
    assert result is not None
    assert load_calls == [True]
