"""Regression tests for native-agent git baseline accounting."""

from __future__ import annotations

import stat
import subprocess
import textwrap
from pathlib import Path

from workers.native_agent_runner import NativeAgentRunRequest, run_native_agent


def _write_fake_binary(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _init_git_repo(repo_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test Runner"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (repo_path / ".seed").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_native_agent_runner_collects_committed_changes_from_start_ref(tmp_path: Path) -> None:
    """Committed native-agent edits should still be reported after a clean working tree."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)

    fake_binary = _write_fake_binary(
        tmp_path / "fake-commit-agent.py",
        """#!/usr/bin/env python3
import subprocess
from pathlib import Path

Path("committed.txt").write_text("committed by native agent\\n", encoding="utf-8")
subprocess.run(["git", "add", "committed.txt"], check=True)
subprocess.run(["git", "commit", "-m", "agent change"], check=True)
print("committed change")
""",
    )

    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="commit the task result",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=10,
        )
    )

    assert result.status == "success"
    assert result.files_changed == ["committed.txt"]
    assert result.diff_text is not None
    assert "diff --git a/committed.txt b/committed.txt" in result.diff_text
    assert "committed by native agent" in result.diff_text
    assert "native-agent-diff" in {artifact.name for artifact in result.artifacts}


def test_collect_diff_text_since_ref_omits_head_without_base_ref(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Unborn repositories should not receive HEAD when no baseline ref exists."""
    calls: list[list[str]] = []

    def _fake_run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    from workers.native_agent_artifacts import _collect_diff_text_since_ref

    assert (
        _collect_diff_text_since_ref(
            repo_path=tmp_path,
            base_ref=None,
            timeout_seconds=10,
        )
        is None
    )
    assert calls == [["git", "-C", str(tmp_path), "diff", "--no-color", "--", "."]]
