"""Explicit non-production process runners for provider parser tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from sandbox.native_agent_executor import NativeAgentExecution


class LocalNativeAgentRunner:
    """Run local fake CLIs only when a test explicitly injects this double."""

    def run(self, **kwargs: object) -> NativeAgentExecution:
        command = kwargs["command"]
        prompt = kwargs["prompt"]
        workspace = kwargs["workspace"]
        artifact_root = kwargs["artifact_root"]
        environment = kwargs["environment"]
        timeout_seconds = kwargs["timeout_seconds"]
        assert isinstance(command, list)
        assert prompt is None or isinstance(prompt, str)
        assert isinstance(artifact_root, Path)
        manifest_path = artifact_root / "native-isolation-manifest.json"
        artifact_root.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps({"execution_backend": "test-local-runner"}), encoding="utf-8"
        )
        completed = subprocess.run(
            command,
            input=prompt,
            stdin=subprocess.DEVNULL if prompt is None else None,
            cwd=workspace.repo_path,
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return NativeAgentExecution(completed, "completed", manifest_path)
