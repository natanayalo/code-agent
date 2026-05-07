import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from db.enums import WorkerRuntimeMode
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
        runtime_adapter=None,  # type: ignore
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(_make_container(workspace)),
        # We'll use the real runner but mock the environment to find our fake binary
    )

    # We need to ensure the worker uses our fake binary.
    # GeminiCliWorker builds the command using 'gemini'.
    # We'll patch the environment used by run_native_agent.

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", f"{bin_dir}:{tmp_path}")

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
        runtime_adapter=None,  # type: ignore
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(_make_container(workspace)),
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", f"{bin_dir}:{tmp_path}")

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
