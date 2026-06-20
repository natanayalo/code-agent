"""Unit tests for Antigravity CLI native adapter behavior."""

from __future__ import annotations

import json
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from workers.antigravity_cli_adapter import (
    AntigravityCliRuntimeAdapter,
    build_antigravity_settings,
    write_antigravity_settings,
)
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
    seed_file = repo_path / "notes.txt"
    seed_file.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_antigravity_adapter_from_env_builds_prompt_command_and_scoped_env(tmp_path: Path) -> None:
    adapter = AntigravityCliRuntimeAdapter.from_env(
        {
            "CODE_AGENT_ANTIGRAVITY_CLI_BIN": "/opt/bin/agy",
            "CODE_AGENT_ANTIGRAVITY_MODEL": "gemini-3-pro",
            "CODE_AGENT_ANTIGRAVITY_TIMEOUT_SECONDS": "42",
            "CODE_AGENT_ANTIGRAVITY_TOOL_PERMISSION": "strict",
            "CODE_AGENT_ANTIGRAVITY_ARTIFACT_REVIEW_POLICY": "manual",
            "XDG_RUNTIME_DIR": "/tmp/runtime",
            "GOOGLE_API_KEY": "drop-me",
        }
    )
    command = adapter.build_native_command(
        prompt="do the work",
        cwd=tmp_path,
    )

    assert command == [
        "/opt/bin/agy",
        "-p",
        "do the work",
        "--cwd",
        str(tmp_path),
        "--model",
        "gemini-3-pro",
    ]
    assert adapter.tool_permission == "strict"
    assert adapter.artifact_review_policy == "asks-for-review"
    assert adapter.env == {"XDG_RUNTIME_DIR": "/tmp/runtime"}


def test_antigravity_settings_generation_merges_workspace_settings(tmp_path: Path) -> None:
    agent_home = tmp_path / ".agent_home"
    settings_dir = agent_home / ".gemini" / "antigravity-cli"
    settings_dir.mkdir(parents=True)
    settings_path = settings_dir / "settings.json"
    settings_path.write_text('{"theme":"dark","toolPermission":"request-review"}', encoding="utf-8")

    written_path = write_antigravity_settings(
        agent_home=agent_home,
        tool_permission="proceed-in-sandbox",
        artifact_review_policy="agent-decides",
        enable_terminal_sandbox=True,
    )

    assert written_path == settings_path
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "artifactReviewPolicy": "agent-decides",
        "enableTerminalSandbox": True,
        "theme": "dark",
        "toolPermission": "proceed-in-sandbox",
    }


def test_antigravity_settings_ignores_unreadable_existing_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent_home = tmp_path / ".agent_home"
    settings_dir = agent_home / ".gemini" / "antigravity-cli"
    settings_dir.mkdir(parents=True)
    settings_path = settings_dir / "settings.json"
    settings_path.write_text('{"theme":"dark"}', encoding="utf-8")
    original_read_text = Path.read_text

    def _read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == settings_path:
            raise OSError("settings file is locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)

    written_path = write_antigravity_settings(
        agent_home=agent_home,
        tool_permission="strict",
        artifact_review_policy="always-proceed",
        enable_terminal_sandbox=False,
    )

    assert written_path == settings_path
    assert json.loads(original_read_text(settings_path, encoding="utf-8")) == {
        "artifactReviewPolicy": "always-proceed",
        "enableTerminalSandbox": False,
        "toolPermission": "strict",
    }


def test_antigravity_settings_reject_invalid_tool_permission() -> None:
    with pytest.raises(ValueError, match="Invalid Antigravity tool permission"):
        build_antigravity_settings(
            tool_permission="launch-missiles",
            artifact_review_policy="auto",
            enable_terminal_sandbox=True,
        )


def test_antigravity_settings_defaults_whitespace_only_values() -> None:
    assert build_antigravity_settings(
        tool_permission="   ",
        artifact_review_policy="\t\n",
        enable_terminal_sandbox=True,
    ) == {
        "artifactReviewPolicy": "agent-decides",
        "enableTerminalSandbox": True,
        "toolPermission": "proceed-in-sandbox",
    }


def test_antigravity_settings_maps_legacy_artifact_review_policy_names() -> None:
    assert (
        build_antigravity_settings(
            tool_permission="strict",
            artifact_review_policy="auto",
            enable_terminal_sandbox=True,
        )["artifactReviewPolicy"]
        == "agent-decides"
    )
    assert (
        build_antigravity_settings(
            tool_permission="strict",
            artifact_review_policy="manual",
            enable_terminal_sandbox=True,
        )["artifactReviewPolicy"]
        == "asks-for-review"
    )


def test_antigravity_settings_reject_invalid_artifact_review_policy() -> None:
    with pytest.raises(ValueError, match="Invalid Antigravity artifact review policy"):
        build_antigravity_settings(
            tool_permission="strict",
            artifact_review_policy="surprise-me",
            enable_terminal_sandbox=True,
        )


def test_fake_agy_success_collects_json_log_diff_and_changed_files(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_agy = _write_fake_binary(
        tmp_path / "agy",
        """#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("-p", dest="prompt", required=True)
parser.add_argument("--cwd", required=True)
parser.add_argument("--model")
args = parser.parse_args()

if sys.stdin.read():
    raise SystemExit("stdin should be empty")
if args.cwd != str(Path.cwd()):
    raise SystemExit("cwd mismatch")

notes = Path("notes.txt")
notes.write_text(notes.read_text(encoding="utf-8") + "after\\n", encoding="utf-8")
print(json.dumps({"response": {"status": "passed", "summary": args.prompt}}))
""",
    )
    adapter = AntigravityCliRuntimeAdapter(executable=str(fake_agy), model="gemini-3-pro")
    prompt = "apply the fake agy edit"
    log_file = tmp_path / "agy.log"

    result = run_native_agent(
        NativeAgentRunRequest(
            command=adapter.build_native_command(
                prompt=prompt,
                cwd=repo_path,
            ),
            prompt=prompt,
            repo_path=repo_path,
            workspace_path=tmp_path,
            stdin_prompt=False,
            command_redactions=[prompt],
            response_format="json",
            timeout_seconds=10,
            events_path=log_file,
        )
    )

    assert result.status == "success"
    assert result.json_payload == {"status": "passed", "summary": prompt}
    assert result.files_changed == ["notes.txt"]
    assert result.diff_text is not None
    assert "after" in result.diff_text
    assert prompt not in result.command
    assert "[REDACTED]" in result.command
    assert {artifact.name for artifact in result.artifacts} == {
        "native-agent-stdout",
        "native-agent-stderr",
        "native-agent-diff",
    }


def test_fake_agy_no_change_success_has_empty_changed_files(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_agy = _write_fake_binary(
        tmp_path / "agy",
        """#!/usr/bin/env python3
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("-p", required=True)
parser.add_argument("--cwd", required=True)
parser.parse_args()
print("No changes needed.")
""",
    )
    adapter = AntigravityCliRuntimeAdapter(executable=str(fake_agy))

    result = run_native_agent(
        NativeAgentRunRequest(
            command=adapter.build_native_command(
                prompt="inspect only",
                cwd=repo_path,
            ),
            prompt="inspect only",
            repo_path=repo_path,
            workspace_path=tmp_path,
            stdin_prompt=False,
            command_redactions=["inspect only"],
            timeout_seconds=10,
        )
    )

    assert result.status == "success"
    assert result.final_message == "No changes needed."
    assert result.files_changed == []
    assert result.diff_text is None


@pytest.mark.parametrize(
    ("stderr", "expected_summary"),
    [
        ("authentication failed: keyring is locked", "Native agent command exited with code 2."),
        ("permission denied by tool permission policy", "Native agent command exited with code 3."),
    ],
)
def test_fake_agy_auth_and_permission_failures_surface_stderr(
    tmp_path: Path,
    stderr: str,
    expected_summary: str,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    exit_code = "2" if "authentication" in stderr else "3"
    fake_agy = _write_fake_binary(
        tmp_path / "agy",
        f"""#!/usr/bin/env python3
import sys

print({stderr!r}, file=sys.stderr)
raise SystemExit({exit_code})
""",
    )
    adapter = AntigravityCliRuntimeAdapter(executable=str(fake_agy))

    result = run_native_agent(
        NativeAgentRunRequest(
            command=adapter.build_native_command(
                prompt="run",
                cwd=repo_path,
            ),
            prompt="run",
            repo_path=repo_path,
            workspace_path=tmp_path,
            stdin_prompt=False,
            command_redactions=["run"],
            timeout_seconds=10,
        )
    )

    assert result.status == "failure"
    assert result.summary == expected_summary
    assert stderr in result.stderr


def test_fake_agy_timeout_is_structured(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_agy = _write_fake_binary(
        tmp_path / "agy",
        """#!/usr/bin/env python3
import time

time.sleep(2)
""",
    )
    adapter = AntigravityCliRuntimeAdapter(executable=str(fake_agy))

    result = run_native_agent(
        NativeAgentRunRequest(
            command=adapter.build_native_command(
                prompt="slow run",
                cwd=repo_path,
            ),
            prompt="slow run",
            repo_path=repo_path,
            workspace_path=tmp_path,
            stdin_prompt=False,
            command_redactions=["slow run"],
            timeout_seconds=1,
        )
    )

    assert result.status == "error"
    assert result.timed_out is True
    assert result.summary == "Native agent command timed out after 1s."
