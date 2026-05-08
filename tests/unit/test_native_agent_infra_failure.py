"""Unit tests for infra-failure (shell crash) detection in the native agent runner."""

from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest

from workers.failure_taxonomy import classify_failure_kind
from workers.native_agent_runner import NativeAgentRunRequest, run_native_agent


def _write_fake_binary(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def test_native_agent_runner_detects_segfault(tmp_path: Path, repo_path: Path) -> None:
    """A segmentation fault in stderr should be flagged as an infra error."""
    fake_binary = _write_fake_binary(
        tmp_path / "fake-segfault.py",
        """#!/usr/bin/env python3
import sys
print("stdout content")
print("Segmentation fault (core dumped)", file=sys.stderr)
sys.exit(139)
""",
    )

    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="task",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=10,
        )
    )

    # Currently it would be "failure", we want "error"
    assert result.status == "error"
    assert "SANDBOX_INFRA" in result.summary
    assert "segmentation fault" in result.summary.lower()


def test_native_agent_runner_detects_oom(tmp_path: Path, repo_path: Path) -> None:
    """An out-of-memory error in stderr should be flagged as an infra error."""
    fake_binary = _write_fake_binary(
        tmp_path / "fake-oom.py",
        """#!/usr/bin/env python3
import sys
print("Killed", file=sys.stderr)
sys.exit(137)
""",
    )

    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="task",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=10,
        )
    )

    assert result.status == "error"
    assert "SANDBOX_INFRA" in result.summary
    assert "killed" in result.summary.lower()


def test_failure_taxonomy_classifies_infra_crash() -> None:
    """The taxonomy should recognize crash markers in the summary."""
    # Direct check of taxonomy logic
    kind = classify_failure_kind(
        status="error",
        summary="SANDBOX_INFRA: detected shell crash (Segmentation fault)",
    )
    assert kind == "sandbox_infra"

    # Check that OOM is also caught if it appears in the summary
    kind = classify_failure_kind(
        status="error",
        summary="Native agent run failed: out of memory",
    )
    assert kind == "sandbox_infra"
