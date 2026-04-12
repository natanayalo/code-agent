"""Unit tests for structured worker prompt assembly."""

from __future__ import annotations

from pathlib import Path

import pytest

from workers.base import WorkerRequest
from workers.prompt import (
    DEFAULT_AGENTS_MAX_CHARACTERS,
    build_repo_context_section,
    build_system_prompt,
    build_task_context_section,
    build_workspace_directory_listing,
    read_workspace_agents_guidance,
)


def test_build_system_prompt_includes_all_expected_sections(tmp_path: Path) -> None:
    """The prompt should assemble role, tools, repo, task, and workflow sections."""
    (tmp_path / "AGENTS.md").write_text("Prefer small diffs.\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "workers").mkdir()
    (tmp_path / "workers" / "gemini_cli_worker.py").write_text("pass\n", encoding="utf-8")

    request = WorkerRequest(
        session_id="session-46",
        repo_url="https://token@github.com/example/repo.git",
        branch="main",
        task_text="Build the structured system prompt module",
        memory_context={"project": "code-agent"},
        constraints={"destructive_actions": "require approval"},
        budget={"max_iterations": 12},
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "## Role" in prompt
    assert "## Available Tools" in prompt
    assert "## Repo Context" in prompt
    assert "## Task Context" in prompt
    assert "## Workflow Instructions" in prompt
    assert "`execute_bash`" in prompt
    assert "Required permission: `workspace_write`" in prompt
    assert "Default timeout: `60s`" in prompt
    assert "Expected artifacts: `stdout`, `stderr`, `changed_files`" in prompt
    assert "AGENTS.md guidance:" in prompt
    assert "Prefer small diffs." in prompt
    assert "README.md" in prompt
    assert "workers/" in prompt
    assert "workers/gemini_cli_worker.py" in prompt
    assert "Repository URL: https://***@github.com/example/repo.git" in prompt
    assert '"max_iterations": 12' in prompt
    assert "Use the available tools with focused commands and targeted reads" in prompt
    assert "treat `repo_url` as the clone source" in prompt
    assert "avoid dumping large files or verbose output" in prompt
    assert "If a command fails" in prompt
    assert "narrow long or truncated results" in prompt
    assert "Base the next step on command exit codes" in prompt


def test_build_system_prompt_includes_build_test_section_from_repo_config(tmp_path: Path) -> None:
    """Build/test context should be injected when common config files are present."""
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"pytest -q","lint":"ruff check ."}}',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo"',
                "[project.scripts]",
                'demo = "apps.main:run"',
                "[tool.pytest.ini_options]",
                'addopts = "-q"',
                "[tool.ruff]",
                "line-length = 100",
                "[tool.mypy]",
                "strict = true",
            ]
        ),
        encoding="utf-8",
    )
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "ci.yml").write_text(
        "\n".join(
            [
                "on:",
                "  push:",
                "  pull_request:",
                "jobs:",
                "  test:",
                "    runs-on: ubuntu-latest",
                "  lint:",
                "    runs-on: ubuntu-latest",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "CONTRIBUTING.md").write_text(
        "\n".join(
            [
                "# Contributing",
                "- `.venv/bin/pytest -q`",
                "- `.venv/bin/pre-commit run --all-files`",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text(
        "\n".join(
            [
                "FROM python:3.12-slim",
                'ENTRYPOINT ["python", "-m", "uvicorn"]',
                'CMD ["apps.api.main:app"]',
            ]
        ),
        encoding="utf-8",
    )

    prompt = build_system_prompt(WorkerRequest(task_text="Inspect project checks"), tmp_path)

    assert "## Build & Test" in prompt
    assert "package.json scripts: lint=" in prompt
    assert 'test="pytest -q"' in prompt
    assert "pyproject.toml [project.scripts]" in prompt
    assert "pyproject.toml [tool.pytest.ini_options]" in prompt
    assert "pyproject.toml [tool.ruff]" in prompt
    assert "pyproject.toml [tool.mypy]" in prompt
    assert ".github/workflows/ci.yml: on=push, pull_request; jobs=test, lint" in prompt
    assert "CONTRIBUTING.md commands:" in prompt
    assert ".venv/bin/pytest -q" in prompt
    assert "Dockerfile: base=python:3.12-slim" in prompt


def test_build_system_prompt_omits_build_test_section_without_recognized_files(
    tmp_path: Path,
) -> None:
    """Build/test context should be omitted when no known config source exists."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")

    prompt = build_system_prompt(WorkerRequest(task_text="Inspect project checks"), tmp_path)

    assert "## Build & Test" not in prompt


def test_build_system_prompt_shares_repo_guidance_budget_with_build_test_section(
    tmp_path: Path,
) -> None:
    """Build/test context should be suppressed when AGENTS guidance consumes the budget."""
    (tmp_path / "AGENTS.md").write_text(
        "A" * (DEFAULT_AGENTS_MAX_CHARACTERS + 32),
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"pytest -q"}}',
        encoding="utf-8",
    )

    prompt = build_system_prompt(WorkerRequest(task_text="Inspect project checks"), tmp_path)

    assert "AGENTS.md guidance:" in prompt
    assert "... (truncated)" in prompt
    assert "## Build & Test" not in prompt


def test_build_repo_context_section_skips_missing_agents_file(tmp_path: Path) -> None:
    """Missing AGENTS.md should not break repo context rendering."""
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "api.py").write_text("app = object()\n", encoding="utf-8")

    section = build_repo_context_section(tmp_path)

    assert "## Repo Context" in section
    assert "apps/" in section
    assert "apps/api.py" in section
    assert "AGENTS.md guidance:" not in section


def test_read_workspace_agents_guidance_truncates_long_agents_file(tmp_path: Path) -> None:
    """Large AGENTS.md files should be truncated before prompt injection."""
    (tmp_path / "AGENTS.md").write_text("0123456789ABCDEFGHIJ", encoding="utf-8")

    guidance = read_workspace_agents_guidance(tmp_path, max_characters=10)

    assert guidance == "0123456789\n... (truncated)"


def test_build_repo_context_section_includes_agents_asset_summaries(tmp_path: Path) -> None:
    """Repo context should include bounded summaries from .agents markdown assets."""
    skills_dir = tmp_path / ".agents" / "skills" / "start-task"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: start-task",
                "description: Start the next implementation slice safely.",
                "---",
                "",
                "# Start Task",
            ]
        ),
        encoding="utf-8",
    )
    workflows_dir = tmp_path / ".agents" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "release.md").write_text(
        "# Release workflow\nUse this for release-day checks.\n",
        encoding="utf-8",
    )

    section = build_repo_context_section(tmp_path)

    assert ".agents guidance:" in section
    assert (
        "skills/start-task/SKILL.md: start-task - " "Start the next implementation slice safely."
    ) in section
    assert "workflows/release.md: release - Release workflow" in section


def test_build_repo_context_section_enforces_shared_guidance_budget(tmp_path: Path) -> None:
    """AGENTS.md and .agents guidance should share one bounded character budget."""
    (tmp_path / "AGENTS.md").write_text(
        "A" * (DEFAULT_AGENTS_MAX_CHARACTERS + 50),
        encoding="utf-8",
    )
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "tiny.md").write_text("tiny summary\n", encoding="utf-8")

    section = build_repo_context_section(tmp_path)

    assert "AGENTS.md guidance:" in section
    assert "... (truncated)" in section
    assert ".agents guidance:" not in section


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


def test_build_workspace_directory_listing_skips_inaccessible_subdirectories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable subdirectories should not crash prompt construction."""
    blocked_dir = tmp_path / "blocked"
    blocked_dir.mkdir()
    (tmp_path / "safe.txt").write_text("ok\n", encoding="utf-8")

    original_iterdir = Path.iterdir

    def fake_iterdir(self: Path):  # type: ignore[no-untyped-def]
        if self == blocked_dir:
            raise PermissionError("blocked")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)

    listing = build_workspace_directory_listing(tmp_path, max_depth=2)

    assert "blocked/" in listing
    assert "safe.txt" in listing


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


def test_build_task_context_section_masks_repo_url_credentials() -> None:
    """Inline repository credentials should be masked in prompt context."""
    section = build_task_context_section(
        WorkerRequest(
            task_text="Inspect the repository",
            repo_url="https://token@github.com/example/repo.git",
        )
    )

    assert "Repository URL: https://***@github.com/example/repo.git" in section
    assert "token@github.com" not in section


def test_build_task_context_section_handles_mixed_type_sets() -> None:
    """Mixed-type sets should be normalized without raising during JSON rendering."""
    section = build_task_context_section(
        WorkerRequest(
            task_text="Inspect the repository",
            memory_context={"tags": {3, "alpha", ("nested", 1)}},
        )
    )

    assert '"tags"' in section
    assert '"alpha"' in section
    assert "nested" in section
