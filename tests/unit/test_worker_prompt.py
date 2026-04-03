"""Unit tests for structured worker prompt assembly."""

from __future__ import annotations

from pathlib import Path

from workers.base import WorkerRequest
from workers.prompt import (
    build_repo_context_section,
    build_system_prompt,
    build_task_context_section,
    build_workspace_directory_listing,
)


def test_build_system_prompt_includes_all_expected_sections(tmp_path: Path) -> None:
    """The prompt should assemble role, tools, repo, task, and workflow sections."""
    (tmp_path / "AGENTS.md").write_text("Prefer small diffs.\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "workers").mkdir()
    (tmp_path / "workers" / "claude_worker.py").write_text("pass\n", encoding="utf-8")

    request = WorkerRequest(
        session_id="session-46",
        repo_url="https://token@github.com/example/repo.git",
        branch="main",
        task_text="Build the structured system prompt module",
        memory_context={"project": "code-agent"},
        constraints={"destructive_actions": "require approval"},
        budget={"max_iterations": 12},
    )

    prompt = build_system_prompt(request, tmp_path, available_tools=["execute_bash"])

    assert "## Role" in prompt
    assert "## Available Tools" in prompt
    assert "## Repo Context" in prompt
    assert "## Task Context" in prompt
    assert "## Workflow Instructions" in prompt
    assert "`execute_bash`" in prompt
    assert "AGENTS.md guidance:" in prompt
    assert "Prefer small diffs." in prompt
    assert "README.md" in prompt
    assert "workers/" in prompt
    assert "workers/claude_worker.py" in prompt
    assert "Repository URL: https://***@github.com/example/repo.git" in prompt
    assert '"max_iterations": 12' in prompt


def test_build_repo_context_section_skips_missing_agents_file(tmp_path: Path) -> None:
    """Missing AGENTS.md should not break repo context rendering."""
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "api.py").write_text("app = object()\n", encoding="utf-8")

    section = build_repo_context_section(tmp_path)

    assert "## Repo Context" in section
    assert "apps/" in section
    assert "apps/api.py" in section
    assert "AGENTS.md guidance:" not in section


def test_build_workspace_directory_listing_truncates_when_entry_budget_is_exceeded(
    tmp_path: Path,
) -> None:
    """Large workspace listings should stay bounded and signal truncation."""
    for index in range(5):
        (tmp_path / f"file_{index}.txt").write_text("data\n", encoding="utf-8")

    listing = build_workspace_directory_listing(tmp_path, max_entries=3, max_depth=1)

    assert "file_0.txt" in listing
    assert "file_1.txt" in listing
    assert "file_2.txt" in listing
    assert "... (truncated)" in listing


def test_build_task_context_section_omits_empty_optional_context() -> None:
    """Empty optional maps should not create empty JSON blocks in the prompt."""
    section = build_task_context_section(
        WorkerRequest(
            task_text="Inspect the repository",
            repo_url="https://github.com/example/repo.git",
        )
    )

    assert "Task text: Inspect the repository" in section
    assert "Repository URL: https://github.com/example/repo.git" in section
    assert "Memory context:" not in section
    assert "Constraints:" not in section
    assert "Budget:" not in section
