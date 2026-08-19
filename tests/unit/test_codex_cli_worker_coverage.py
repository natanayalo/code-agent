"""Unit tests to increase coverage of codex_cli_worker_native."""

from unittest.mock import MagicMock

from sandbox.secrets import SecretRef
from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle
from workers.base import WorkerRequest, WorkerRuntimeMode
from workers.cli_runtime_types import CliRuntimeSettings
from workers.codex_cli_worker_native import CodexCliWorkerNativeMixin


def test_codex_native_prepare_request_secret_refs(monkeypatch, tmp_path):
    class FakeCodexWorker(CodexCliWorkerNativeMixin):
        def __init__(self):
            pass

    worker = FakeCodexWorker()
    worker.tool_registry = MagicMock()
    worker.runtime_adapter = MagicMock()
    worker.native_event_capture_enabled = False
    worker.build_memory_context_string = lambda *args, **kwargs: ""
    worker.is_native_mode = lambda *args, **kwargs: True
    worker._build_native_command = lambda *args, **kwargs: (["echo"], {})
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
        exposure_policy = "sandbox_env"

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

    monkeypatch.setattr(
        "sandbox.provider_bootstrap.ProviderBootstrapLoader.load", lambda *args: FakeBootstrap()
    )
    monkeypatch.setattr("sandbox.secrets.SecretRegistry", FakeRegistry)
    monkeypatch.setattr("sandbox.capability.CapabilityGrantFactory", FakeGrant)
    monkeypatch.setattr("sandbox.secrets.SecretResolver", FakeResolver)
    monkeypatch.setattr("sandbox.trusted_context.TrustedSandboxExecutionContext", FakeContext)
    monkeypatch.setattr(
        "sandbox.capability.validate_grant_for_execution", lambda *args, **kwargs: None
    )

    result, md = worker._prepare_native_agent_run_request(
        request, workspace, runtime_settings, WorkerRuntimeMode.NATIVE_AGENT, None
    )
    assert result is not None
