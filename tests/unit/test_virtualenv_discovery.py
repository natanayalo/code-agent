"""Unit tests for virtualenv interpreter resolution and prompt guidance."""

from __future__ import annotations

from pathlib import Path

from sandbox.native_agent_executor import (
    build_executor_environment,
    stage_agent_home_shell_environment,
)
from workers.base import WorkerRequest
from workers.prompt import build_workflow_instructions_section


def test_stage_agent_home_shell_environment_creates_expected_files(tmp_path: Path) -> None:
    agent_home = tmp_path / "agent_home"
    agent_home.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    env_script = stage_agent_home_shell_environment(agent_home, repo_path)
    assert env_script == agent_home / ".code_agent_env.sh"
    assert env_script.is_file()

    expected_bin = str(repo_path.resolve() / ".venv" / "bin")
    content = env_script.read_text(encoding="utf-8")
    assert expected_bin in content
    assert f'if [ -d "{expected_bin}" ]; then' in content
    assert f'export PATH="{expected_bin}:$PATH"' in content

    profile = agent_home / ".profile"
    bashrc = agent_home / ".bashrc"
    assert profile.is_file()
    assert bashrc.is_file()
    assert '. "$HOME/.code_agent_env.sh"' in profile.read_text(encoding="utf-8")
    assert '. "$HOME/.code_agent_env.sh"' in bashrc.read_text(encoding="utf-8")


def test_build_executor_environment_allows_bash_env() -> None:
    raw_env = {
        "BASH_ENV": "/path/to/.code_agent_env.sh",
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/code-agent",
        "UNALLOWED_KEY": "secret_value",
    }
    scoped = build_executor_environment(raw_env)
    assert scoped["BASH_ENV"] == "/path/to/.code_agent_env.sh"
    assert scoped["PATH"] == "/usr/bin:/bin"
    assert scoped["HOME"] == "/home/code-agent"
    assert "UNALLOWED_KEY" not in scoped


def test_build_workflow_instructions_includes_interpreter_verification_guidance() -> None:
    request = WorkerRequest(
        session_id="session-1",
        task_id="task-1",
        repo_url="https://github.com/example/repo.git",
        branch="main",
        task_text="Run tests and fix issues",
    )
    instructions = build_workflow_instructions_section(request)
    assert "Check the repository interpreter (.venv/bin/python or .venv/bin/<tool>)" in instructions
    assert "before reporting missing dependencies or environment blockers" in instructions
