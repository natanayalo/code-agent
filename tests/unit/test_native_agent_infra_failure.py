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


def test_native_agent_runner_detects_illegal_instruction(tmp_path: Path, repo_path: Path) -> None:
    """An illegal instruction in stderr should be flagged as an infra error."""
    fake_binary = _write_fake_binary(
        tmp_path / "fake-ill.py",
        """#!/usr/bin/env python3
import sys
print("Illegal instruction: 4", file=sys.stderr)
sys.exit(132)
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
    assert "illegal instruction" in result.summary.lower()


def test_native_agent_runner_ignores_false_positive_killed(tmp_path: Path, repo_path: Path) -> None:
    """Words containing 'killed' but not being a crash should not be flagged."""
    fake_binary = _write_fake_binary(
        tmp_path / "fake-fulfilled.py",
        """#!/usr/bin/env python3
import sys
print("The request was fulfilled successfully.", file=sys.stderr)
sys.exit(1)
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

    # Should be "failure" because exit code 1, but NOT "error"
    assert result.status == "failure"
    assert "SANDBOX_INFRA" not in result.summary


def test_native_agent_runner_ignores_success_with_markers(tmp_path: Path, repo_path: Path) -> None:
    """Successful runs should not be scanned for crash markers."""
    fake_binary = _write_fake_binary(
        tmp_path / "fake-success-marker.py",
        """#!/usr/bin/env python3
import sys
print("Segmentation fault", file=sys.stderr)
sys.exit(0)
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

    assert result.status == "success"
    assert "SANDBOX_INFRA" not in result.summary


def test_native_agent_runner_detects_killed_by_signal_precedence(
    tmp_path: Path, repo_path: Path
) -> None:
    """Specific markers should take precedence over generic ones (killed by signal vs killed)."""
    fake_binary = _write_fake_binary(
        tmp_path / "fake-kbs.py",
        """#!/usr/bin/env python3
import sys
print("Killed by signal 9", file=sys.stderr)
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
    # Should be the more specific one
    assert "(killed by signal)" in result.summary.lower()
    assert "(killed)" not in result.summary.lower()


def test_native_agent_runner_detects_signal_exit_code(tmp_path: Path, repo_path: Path) -> None:
    """If stderr is empty but exit code matches a signal, it should be flagged as infra error."""
    fake_binary = _write_fake_binary(
        tmp_path / "fake-sigsegv-silent.py",
        """#!/usr/bin/env python3
import sys
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

    assert result.status == "error"
    assert "SANDBOX_INFRA" in result.summary
    assert "(sigsegv)" in result.summary.lower()


def test_native_agent_runner_detects_negative_return_code(
    tmp_path: Path, repo_path: Path, monkeypatch
) -> None:
    """If returncode is negative (direct signal), it should be flagged as infra error."""
    import subprocess

    fake_binary = _write_fake_binary(
        tmp_path / "fake-direct-signal.py",
        """#!/usr/bin/env python3
import sys
print("ok")
""",
    )

    # Mock subprocess.run to return a negative returncode
    original_run = subprocess.run

    def mock_run(*args, **kwargs):
        res = original_run(*args, **kwargs)
        return subprocess.CompletedProcess(
            args=res.args,
            returncode=-11,  # SIGSEGV
            stdout=res.stdout,
            stderr=res.stderr,
        )

    monkeypatch.setattr(subprocess, "run", mock_run)

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
    assert "(sigsegv)" in result.summary.lower()


def test_failure_taxonomy_precedence_infra_over_test() -> None:
    """Infra crashes should take precedence over test failures in classification."""
    # If a summary contains both a test failure marker and an infra crash marker,
    # it should be classified as sandbox_infra.
    summary = "Tests failed: AssertionError. Process killed by SIGKILL."
    kind = classify_failure_kind(
        status="error",
        summary=summary,
    )
    assert kind == "sandbox_infra"


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

    # Check for false positive prevention (fulfilled)
    kind = classify_failure_kind(
        status="error",
        summary="The request was fulfilled successfully.",
    )
    assert kind == "unknown"

    # Check for false positive prevention for other markers (unaborted, omnibus)
    from workers.failure_taxonomy import find_infra_failure_marker

    assert find_infra_failure_marker("The process was unaborted.") is None
    assert find_infra_failure_marker("The omnibus was parked.") is None
    assert find_infra_failure_marker("aborted") == "aborted"
    assert find_infra_failure_marker("bus error") == "bus error"
