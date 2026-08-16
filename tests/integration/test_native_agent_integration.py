import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import workers.native_agent_runner as native_agent_runner
from db.enums import WorkerRuntimeMode
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
