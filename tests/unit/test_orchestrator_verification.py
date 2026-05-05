"""Unit tests for independent verifier helpers."""

from __future__ import annotations

import subprocess

import orchestrator.verification as verification_module
from orchestrator.state import OrchestratorState


def _state_with_workspace(tmp_path, *, verification_commands: list[str]) -> OrchestratorState:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("demo\n", encoding="utf-8")
    return OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "task_spec": {"goal": "demo", "verification_commands": verification_commands},
            "result": {
                "status": "success",
                "artifacts": [
                    {
                        "name": "workspace",
                        "uri": workspace.as_uri(),
                        "artifact_type": "workspace",
                    }
                ],
            },
        }
    )


def test_run_independent_verifier_reports_timeout_as_timeout(tmp_path, monkeypatch) -> None:
    state = _state_with_workspace(tmp_path, verification_commands=["ls"])

    def _raise_timeout(*args, **kwargs):  # noqa: ANN002, ANN003
        command = args[0] if args else kwargs.get("command", "unknown")
        timeout_seconds = kwargs.get("timeout_seconds", 0)
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout_seconds)

    monkeypatch.setattr(verification_module, "_run_command", _raise_timeout)

    status, summary = verification_module.run_independent_verifier(state)

    assert status == "failed"
    assert "timed out" in summary
    assert "failed unexpectedly" not in summary


def test_run_command_returns_bounded_output_preview(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    returncode, detail = verification_module._run_command(  # noqa: SLF001
        "python -c \"print('x' * 20000)\"",
        workspace_path=workspace,
        timeout_seconds=5,
    )

    assert returncode == 0
    assert len(detail) <= verification_module.DEFAULT_INDEPENDENT_VERIFIER_OUTPUT_PREVIEW_BYTES
