"""Regression coverage for native-agent result validation."""

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
        ["git", "commit", "-m", "seed"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_native_agent_runner_rejects_zero_exit_without_result(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-empty-success.py", "#!/usr/bin/env python3\nraise SystemExit(0)\n"
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
    assert result.summary == "NATIVE_AGENT_EMPTY_RESULT: native agent produced no result."
    assert result.friction_reports[0]["source"] == "tooling"


def test_native_agent_runner_allows_silent_success_for_shell_commands(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-empty-shell-success.py",
        "#!/usr/bin/env python3\nraise SystemExit(0)\n",
    )

    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="set -e\ntrue",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=10,
            require_observable_result=False,
        )
    )

    assert result.status == "success"
    assert result.summary == "Native agent run completed successfully."
    assert result.friction_reports == []


def test_native_agent_runner_rejects_quota_error_with_zero_exit(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-quota-success.py",
        """#!/usr/bin/env python3
print("RESOURCE_EXHAUSTED: Individual quota reached", flush=True)
raise SystemExit(0)
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
    assert result.summary == "NATIVE_AGENT_PROVIDER_FAILURE: provider request was not completed."
    assert result.friction_reports[0]["source"] == "tooling"
