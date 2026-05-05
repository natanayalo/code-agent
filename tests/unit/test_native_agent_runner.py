"""Unit tests for the native-agent one-shot runner."""

from __future__ import annotations

import stat
import subprocess
import textwrap
from pathlib import Path

import workers.native_agent_runner as native_runner
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


def test_native_agent_runner_truncates_stdout_fallback_summary(tmp_path: Path) -> None:
    """Long stdout fallback summaries should be bounded to prevent payload bloat."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    long_stdout = "x" * (native_runner.DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS + 250)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-long-stdout.py",
        f"""#!/usr/bin/env python3
print("{long_stdout}")
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
    assert result.final_message is not None
    assert result.final_message.startswith("[stdout truncated for summary]")
    assert len(result.final_message) <= (
        len("[stdout truncated for summary]\n")
        + native_runner.DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS
    )
    assert result.final_message.endswith(
        "x" * native_runner.DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS
    )


def test_read_final_message_is_bounded(tmp_path: Path) -> None:
    """Final message parsing should cap file reads to a fixed safety budget."""
    final_message_path = tmp_path / "final-message.txt"
    oversized = "a" * (native_runner.DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS + 120)
    final_message_path.write_text(oversized, encoding="utf-8")

    parsed = native_runner._read_final_message(final_message_path)

    assert parsed is not None
    assert parsed.endswith("[final message truncated for safety]")
    assert len(parsed) < len(oversized)


def test_native_agent_runner_returns_structured_error_on_artifact_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Artifact copy/write errors should not crash the caller."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-success.py",
        """#!/usr/bin/env python3
print("ok")
""",
    )

    def _raise_artifact_error(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(native_runner, "_write_artifact", _raise_artifact_error)

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
    assert "failed while collecting artifacts" in result.summary
    assert "disk full" in result.summary
