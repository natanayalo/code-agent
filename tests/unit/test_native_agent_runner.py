"""Unit tests for the native-agent one-shot runner."""

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
    subprocess.run(
        ["git", "init"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
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
    seed_file = repo_path / ".seed"
    if not seed_file.exists():
        seed_file.write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_native_agent_runner_collects_final_message_diff_and_artifacts(tmp_path: Path) -> None:
    """Successful runs should capture final output, git metadata, and artifacts."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    tracked_file = repo_path / "notes.txt"
    tracked_file.write_text("before\n", encoding="utf-8")
    _init_git_repo(repo_path)

    fake_binary = _write_fake_binary(
        tmp_path / "fake-native-agent.py",
        """#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--final-message", required=True)
parser.add_argument("--events", required=True)
parser.add_argument("--touch-file", required=True)
args = parser.parse_args()

prompt = sys.stdin.read()
target = Path(args.touch_file)
target.write_text(target.read_text(encoding="utf-8") + "after\\n", encoding="utf-8")
Path(args.events).write_text('{"event":"turn.completed"}\\n', encoding="utf-8")
Path(args.final_message).write_text(
    json.dumps({"final_output": f"Applied change for: {prompt.strip()}"}),
    encoding="utf-8",
)
print("stdout payload")
print("stderr payload", file=sys.stderr)
""",
    )

    final_message_path = tmp_path / "final-message.json"
    events_path = tmp_path / "events.jsonl"
    result = run_native_agent(
        NativeAgentRunRequest(
            command=[
                str(fake_binary),
                "--final-message",
                str(final_message_path),
                "--events",
                str(events_path),
                "--touch-file",
                str(tracked_file),
            ],
            prompt="implement the task",
            repo_path=repo_path,
            workspace_path=tmp_path,
            final_message_path=final_message_path,
            events_path=events_path,
            timeout_seconds=10,
        )
    )

    assert result.status == "success"
    assert result.exit_code == 0
    assert result.final_message == "Applied change for: implement the task"
    assert result.summary == "Applied change for: implement the task"
    assert result.files_changed == ["notes.txt"]
    assert result.diff_text is not None
    assert "diff --git a/notes.txt b/notes.txt" in result.diff_text
    assert result.stdout.strip() == "stdout payload"
    assert result.stderr.strip() == "stderr payload"

    artifact_names = {artifact.name for artifact in result.artifacts}
    assert artifact_names == {
        "native-agent-stdout",
        "native-agent-stderr",
        "native-agent-events",
        "native-agent-final-message",
        "native-agent-diff",
    }
    for artifact in result.artifacts:
        assert Path(artifact.uri.removeprefix("file://")).is_file()


def test_native_agent_runner_handles_non_zero_exit(tmp_path: Path) -> None:
    """Non-zero native command exits should return failure status."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-exit-failure.py",
        """#!/usr/bin/env python3
import sys
print("failure stdout")
print("failure stderr", file=sys.stderr)
raise SystemExit(7)
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

    assert result.status == "failure"
    assert result.exit_code == 7
    assert result.summary == "Native agent command exited with code 7."
    assert result.final_message == "failure stdout"
    assert len(result.artifacts) == 2
    assert result.artifacts[0].name == "native-agent-stdout"
    assert result.artifacts[1].name == "native-agent-stderr"


def test_native_agent_runner_handles_timeout(tmp_path: Path) -> None:
    """Timeouts should return structured error results."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-timeout.py",
        """#!/usr/bin/env python3
import time
time.sleep(2)
""",
    )

    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="task",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=1,
        )
    )

    assert result.status == "error"
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.summary == "Native agent command timed out after 1s."
    assert len(result.artifacts) == 2
    assert result.artifacts[0].name == "native-agent-stdout"
    assert result.artifacts[1].name == "native-agent-stderr"
