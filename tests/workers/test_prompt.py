"""Unit tests for structured worker prompt assembly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import workers.prompt as prompt
from workers.base import WorkerRequest
from workers.prompt import (
    DEFAULT_AGENTS_MAX_CHARACTERS,
    build_build_test_section,
    build_repo_context_section,
    build_review_prompt,
    build_system_prompt,
    build_task_context_section,
    build_workspace_directory_listing,
    read_workspace_agents_guidance,
    read_workspace_review_guidance,
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


def test_build_review_prompt_includes_repo_and_review_guidance(tmp_path: Path) -> None:
    """Review-mode prompt should include repo guidance and optional REVIEW.md context."""
    (tmp_path / "AGENTS.md").write_text("Keep changes small.\n", encoding="utf-8")
    (tmp_path / "REVIEW.md").write_text("Flag only concrete regressions.\n", encoding="utf-8")
    skills_dir = tmp_path / ".agents" / "skills" / "review"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: review-skill",
                "description: Focus on actionable findings.",
                "---",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"pytest -q"}}',
        encoding="utf-8",
    )

    rendered_prompt = build_review_prompt(
        workspace_path=tmp_path,
        review_context_packet="Packet payload",
        reviewer_kind="independent_reviewer",
        task_text="Review updated auth validation",
    )

    assert "## Review Role" in rendered_prompt
    assert "## Review Guidance" in rendered_prompt
    assert "AGENTS.md guidance:" in rendered_prompt
    assert ".agents guidance:" in rendered_prompt
    assert "REVIEW.md guidance:" in rendered_prompt
    assert "## Build & Test" in rendered_prompt
    assert "## Review Task" in rendered_prompt
    assert "## Output Contract" in rendered_prompt
    assert "## Review Context Packet" in rendered_prompt
    assert "Reviewer kind: independent_reviewer" in rendered_prompt
    assert "Task objective: Review updated auth validation" in rendered_prompt
    assert '"reviewer_kind": "independent_reviewer"' in rendered_prompt
    assert "```json" in rendered_prompt


def test_build_review_prompt_skips_missing_review_file_and_execution_sections(
    tmp_path: Path,
) -> None:
    """Review prompt should not include execution-mode tool-loop instructions."""
    rendered_prompt = build_review_prompt(
        workspace_path=tmp_path,
        review_context_packet="Packet payload",
    )

    assert "REVIEW.md guidance:" not in rendered_prompt
    assert "## Available Tools" not in rendered_prompt
    assert "## Workflow Instructions" not in rendered_prompt
    assert "Use the available tools with focused commands" not in rendered_prompt


def test_build_review_prompt_uses_collision_safe_guidance_fences(tmp_path: Path) -> None:
    """Guidance blocks should use fences longer than embedded backtick runs."""
    (tmp_path / "AGENTS.md").write_text(
        "Use examples:\n```python\nprint('hi')\n```\n",
        encoding="utf-8",
    )

    rendered_prompt = build_review_prompt(
        workspace_path=tmp_path,
        review_context_packet="Packet payload",
    )

    assert "\n```text\n" not in rendered_prompt
    assert "\n````text\n" in rendered_prompt


def test_build_review_prompt_enforces_shared_guidance_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review prompt should share one budget across repo/review guidance and build context."""
    monkeypatch.setattr(prompt, "DEFAULT_REVIEW_GUIDANCE_MAX_CHARACTERS", 120)
    (tmp_path / "AGENTS.md").write_text("A" * 300, encoding="utf-8")
    (tmp_path / "REVIEW.md").write_text("B" * 200, encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"pytest -q"}}', encoding="utf-8")

    rendered_prompt = build_review_prompt(
        workspace_path=tmp_path,
        review_context_packet="Packet payload",
    )

    assert "AGENTS.md guidance:" in rendered_prompt
    assert "## Build & Test" not in rendered_prompt
    assert "REVIEW.md guidance:" not in rendered_prompt


def test_build_review_prompt_accurately_counts_budget_overhead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review prompt should account for header and block separators in budget tracking."""
    # Set a tight budget that leaves exactly enough for one guidance block + header
    # "## Review Guidance" (18 chars) + "\n" (1) = 19
    # "REVIEW.md guidance:\n```text\nCONTENT\n```"
    # label (19) + \n (1) + fence (7) + \n (1) + CONTENT (7) + \n (1) + fence (3) = 39
    # Total = 19 + 39 = 58
    monkeypatch.setattr(prompt, "DEFAULT_REVIEW_GUIDANCE_MAX_CHARACTERS", 58)

    (tmp_path / "REVIEW.md").write_text("CONTENT", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"pytest"}}', encoding="utf-8")

    rendered_prompt = build_review_prompt(
        workspace_path=tmp_path,
        review_context_packet="Packet payload",
    )

    assert "REVIEW.md guidance:" in rendered_prompt
    # Build & Test should be suppressed because budget is exhausted
    assert "## Build & Test" not in rendered_prompt

    # Now test with a budget that allows exactly one char of build context
    monkeypatch.setattr(prompt, "DEFAULT_REVIEW_GUIDANCE_MAX_CHARACTERS", 59)
    rendered_prompt = build_review_prompt(
        workspace_path=tmp_path,
        review_context_packet="Packet payload",
    )
    # This might still be None because build_build_test_section needs some
    # minimal chars for header "## Build & Test"
    # Let's check if it's suppressed.
    assert "## Build & Test" not in rendered_prompt


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


def test_build_system_prompt_accounts_for_guidance_wrapper_overhead_in_budget(
    tmp_path: Path,
) -> None:
    """Repo-context wrapper text should be counted when sharing guidance/build budgets."""
    (tmp_path / "AGENTS.md").write_text(
        "A" * (DEFAULT_AGENTS_MAX_CHARACTERS - 10),
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"pytest -q"}}',
        encoding="utf-8",
    )

    rendered_prompt = build_system_prompt(
        WorkerRequest(task_text="Inspect project checks"), tmp_path
    )

    assert "AGENTS.md guidance:" in rendered_prompt
    assert "## Build & Test" not in rendered_prompt


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


def test_read_workspace_review_guidance_truncates_and_handles_missing(tmp_path: Path) -> None:
    """REVIEW.md guidance should be optional and bounded when present."""
    assert read_workspace_review_guidance(tmp_path) is None
    (tmp_path / "REVIEW.md").write_text("0123456789ABCDEFGHIJ", encoding="utf-8")

    guidance = read_workspace_review_guidance(tmp_path, max_characters=10)

    assert guidance is not None
    assert len(guidance) <= 10
    assert guidance.startswith("\n... (trun")


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
        "skills/start-task/SKILL.md: start-task - Start the next implementation slice safely."
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


def test_build_task_context_section_includes_task_plan_when_present() -> None:
    """Task plans should be rendered in task context when provided by orchestrator."""
    section = build_task_context_section(
        WorkerRequest(
            task_text="Inspect and update architecture",
            task_plan={
                "triggered": True,
                "complexity_reason": "architectural_task",
                "steps": [
                    {
                        "step_id": "1",
                        "title": "Inspect",
                        "expected_outcome": "Find impacted modules",
                    }
                ],
            },
        )
    )

    assert "Task plan:" in section
    assert '"complexity_reason": "architectural_task"' in section
    assert '"step_id": "1"' in section


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


def test_build_build_test_section_handles_makefile_filters_and_truncates_targets(
    tmp_path: Path,
) -> None:
    """Makefile summary should skip special targets and stop at item limit."""
    (tmp_path / "Makefile").write_text(
        "\n".join(
            [
                ".PHONY: test lint",
                "%pattern: ; @true",
                "VERSION := 1",
                "build :",
                "test:all",
                "lint:",
                "lint:",
                "check:",
                "deploy:",
                "format:",
                "verify:",
                "docs:",
                "extra:",
            ]
        ),
        encoding="utf-8",
    )

    section = build_build_test_section(tmp_path)

    assert section is not None
    assert "## Build & Test" in section
    assert "Makefile targets:" in section
    assert ".PHONY" not in section
    assert "%pattern" not in section
    assert "VERSION" not in section
    assert "test" in section
    assert "extra" not in section


def test_summarize_makefile_detects_gnumakefile_and_lowercase_makefile(tmp_path: Path) -> None:
    """Makefile summary should detect GNUmakefile/makefile variants when Makefile is absent."""
    (tmp_path / "GNUmakefile").write_text("build:\n", encoding="utf-8")
    summary = prompt._summarize_makefile(tmp_path)
    assert summary is not None
    assert "build" in summary

    (tmp_path / "GNUmakefile").unlink()
    (tmp_path / "makefile").write_text("test:\n", encoding="utf-8")
    summary = prompt._summarize_makefile(tmp_path)
    assert summary is not None
    assert "test" in summary


def test_extract_makefile_targets_includes_multiple_targets_on_one_rule_line() -> None:
    """Makefile extraction should include each target declared before one colon."""
    contents = "\n".join(
        [
            "test lint: deps",
            "deploy: all",
        ]
    )

    targets = prompt._extract_makefile_targets(contents)

    assert targets == ["test", "lint", "deploy"]


def test_extract_makefile_targets_includes_path_like_targets() -> None:
    """Makefile extraction should keep path-like targets that include forward slashes."""
    contents = "\n".join(
        [
            "bin/app docs/build: deps",
            "release: all",
        ]
    )

    targets = prompt._extract_makefile_targets(contents)

    assert targets == ["bin/app", "docs/build", "release"]


def test_build_build_test_section_handles_invalid_package_and_pyproject_files(
    tmp_path: Path,
) -> None:
    """Invalid config payloads should be ignored without crashing prompt assembly."""
    (tmp_path / "package.json").write_text("{not-json", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("invalid = {", encoding="utf-8")

    section = build_build_test_section(tmp_path)

    assert section is None


def test_summarize_pyproject_config_uses_pytest_fallback_and_skips_non_dict_tools(
    tmp_path: Path,
) -> None:
    """Pyproject summary should include fallback pytest table and skip non-dict tools."""
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.pytest]",
                'addopts = "-q"',
                "[tool.mypy]",
                "strict = true",
            ]
        ),
        encoding="utf-8",
    )

    lines = prompt._summarize_pyproject_config(tmp_path)

    assert any("[tool.pytest]" in line for line in lines)
    assert any("[tool.mypy]" in line for line in lines)
    assert not any("[tool.ruff]" in line for line in lines)


def test_summarize_pyproject_config_skips_empty_pytest_ruff_and_mypy_tables(
    tmp_path: Path,
) -> None:
    """Empty tool tables should not add noisy build/test summary lines."""
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.pytest]",
                "[tool.pytest.ini_options]",
                "[tool.ruff]",
                "[tool.mypy]",
            ]
        ),
        encoding="utf-8",
    )

    lines = prompt._summarize_pyproject_config(tmp_path)

    assert not any("[tool.pytest.ini_options]" in line for line in lines)
    assert not any("[tool.pytest]" in line for line in lines)
    assert not any("[tool.ruff]" in line for line in lines)
    assert not any("[tool.mypy]" in line for line in lines)


def test_summarize_pyproject_config_includes_tool_poetry_scripts(tmp_path: Path) -> None:
    """Poetry script definitions should be summarized when present."""
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.poetry.scripts]",
                'serve = "app.main:run"',
            ]
        ),
        encoding="utf-8",
    )

    lines = prompt._summarize_pyproject_config(tmp_path)

    assert any("[tool.poetry.scripts]" in line for line in lines)
    assert any("serve" in line for line in lines)


def test_extract_yaml_top_level_keys_handles_inline_lists_and_invalid_names() -> None:
    """Workflow-key extraction should parse inline lists and reject invalid identifiers."""
    workflow_text = "\n".join(
        [
            'on: [push, pull_request, "workflow_dispatch", bad$key]',
            "jobs:",
            "  test:",
            "    runs-on: ubuntu-latest",
            '  "lint-job":',
            "    runs-on: ubuntu-latest",
            "  bad$key:",
            "    runs-on: ubuntu-latest",
        ]
    )

    events = prompt._extract_yaml_top_level_keys(workflow_text, root_key="on")
    jobs = prompt._extract_yaml_top_level_keys(workflow_text, root_key="jobs")
    missing = prompt._extract_yaml_top_level_keys(workflow_text, root_key="schedule")

    assert events == ["push", "pull_request", "workflow_dispatch"]
    assert jobs == ["test", "lint-job"]
    assert missing == []


def test_extract_yaml_top_level_keys_accepts_whitespace_before_colon() -> None:
    """Root-key detection should tolerate valid YAML whitespace before the colon."""
    workflow_text = "\n".join(
        [
            "on : [push, pull_request]",
            "jobs :",
            "  test:",
            "    runs-on: ubuntu-latest",
        ]
    )

    events = prompt._extract_yaml_top_level_keys(workflow_text, root_key="on")
    jobs = prompt._extract_yaml_top_level_keys(workflow_text, root_key="jobs")

    assert events == ["push", "pull_request"]
    assert jobs == ["test"]


def test_extract_yaml_top_level_keys_requires_root_level_and_supports_quoted_keys() -> None:
    """Root-key parsing should ignore nested keys and accept quoted top-level keys."""
    workflow_text = "\n".join(
        [
            "name: ci",
            "env:",
            "  on: [push]",
            '"on": [workflow_dispatch]',
            "jobs:",
            "  test:",
            "    steps:",
            "      - name: check",
        ]
    )

    events = prompt._extract_yaml_top_level_keys(workflow_text, root_key="on")

    assert events == ["workflow_dispatch"]


def test_extract_yaml_top_level_keys_handles_inline_list_comments_and_quoted_commas() -> None:
    """Inline list parsing should ignore trailing comments and preserve quoted commas."""
    workflow_text = 'on: ["label,edited", push, workflow_dispatch] # trailing comment'

    events = prompt._extract_yaml_top_level_keys(workflow_text, root_key="on")

    assert events == ["push", "workflow_dispatch"]


def test_extract_yaml_top_level_keys_handles_block_list_comments() -> None:
    """Block list entries should parse even when each item includes a trailing comment."""
    workflow_text = "\n".join(
        [
            "on:",
            "  - push # ci trigger",
            "  - pull_request # pr trigger",
            "jobs:",
            "  test:",
            "    runs-on: ubuntu-latest",
        ]
    )

    events = prompt._extract_yaml_top_level_keys(workflow_text, root_key="on")

    assert events == ["push", "pull_request"]


def test_summarize_github_workflows_skips_unreadable_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable workflow files should be skipped while readable ones remain summarized."""
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    readable_file = workflows_dir / "a.yml"
    unreadable_file = workflows_dir / "b.yml"
    readable_file.write_text("on: [push]\njobs:\n  test:\n", encoding="utf-8")
    unreadable_file.write_text("on: [pull_request]\njobs:\n  lint:\n", encoding="utf-8")

    original_read_text_prefix = prompt._read_text_prefix

    def fake_read_text_prefix(path: Path, *, max_characters: int) -> str:
        if path == unreadable_file:
            raise OSError("denied")
        return original_read_text_prefix(path, max_characters=max_characters)

    monkeypatch.setattr(prompt, "_read_text_prefix", fake_read_text_prefix)

    summaries = prompt._summarize_github_workflows(tmp_path)

    assert len(summaries) == 1
    assert "a.yml" in summaries[0]
    assert "b.yml" not in summaries[0]


def test_summarize_contributing_commands_filters_noise_and_dedupes(tmp_path: Path) -> None:
    """Contributing command extraction should keep actionable unique command hints only."""
    (tmp_path / "CONTRIBUTING.md").write_text(
        "\n".join(
            [
                "# Contributing",
                "Read this guide first.",
                "- `.venv/bin/pytest -q`",
                "- `.venv/bin/pytest -q`",
                "- `echo hello`",
                "- `ruff check .`",
                "- `docker compose up`",
            ]
        ),
        encoding="utf-8",
    )

    summary = prompt._summarize_contributing_commands(tmp_path)

    assert summary is not None
    assert ".venv/bin/pytest -q" in summary
    assert "ruff check ." in summary
    assert "docker compose up" in summary
    assert summary.count(".venv/bin/pytest -q") == 1
    assert "echo hello" not in summary


def test_summarize_contributing_commands_truncates_long_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contributing summary should be bounded to avoid oversized single-line output."""
    monkeypatch.setattr(prompt, "_BUILD_CONTEXT_VALUE_MAX_CHARACTERS", 40)
    (tmp_path / "CONTRIBUTING.md").write_text(
        "\n".join(
            [
                "- `.venv/bin/pytest -q --maxfail=1 --disable-warnings --strict-config`",
                "- `ruff check . --fix --show-fixes --output-format=full`",
            ]
        ),
        encoding="utf-8",
    )

    summary = prompt._summarize_contributing_commands(tmp_path)

    assert summary is not None
    assert "... (truncated)" in summary


def test_summarize_dockerfile_handles_missing_instructions_and_empty_files(
    tmp_path: Path,
) -> None:
    """Dockerfile summary should be None when no actionable instructions are present."""
    (tmp_path / "Dockerfile").write_text("# comment-only\n", encoding="utf-8")
    assert prompt._summarize_dockerfile(tmp_path) is None

    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    summary = prompt._summarize_dockerfile(tmp_path)
    assert summary is not None
    assert "base=python:3.12-slim" in summary


def test_summarize_dockerfile_handles_from_without_image_value(tmp_path: Path) -> None:
    """Malformed FROM lines should not raise and should produce no docker summary."""
    (tmp_path / "Dockerfile").write_text("FROM \n", encoding="utf-8")

    assert prompt._summarize_dockerfile(tmp_path) is None


def test_summarize_dockerfile_prefers_last_effective_instructions(tmp_path: Path) -> None:
    """Docker summary should reflect the final effective FROM/ENTRYPOINT/CMD instructions."""
    (tmp_path / "Dockerfile").write_text(
        "\n".join(
            [
                "FROM python:3.12 as build",
                'ENTRYPOINT ["python", "build.py"]',
                'CMD ["build"]',
                "FROM gcr.io/distroless/python3",
                'ENTRYPOINT ["python", "-m", "app.main"]',
                'CMD ["--serve"]',
            ]
        ),
        encoding="utf-8",
    )

    summary = prompt._summarize_dockerfile(tmp_path)

    assert summary is not None
    assert "base=gcr.io/distroless/python3" in summary
    assert 'entrypoint=["python", "-m", "app.main"]' in summary
    assert 'cmd=["--serve"]' in summary
    assert "python:3.12 as build" not in summary


def test_summarize_dockerfile_handles_tabs_and_continuations(tmp_path: Path) -> None:
    """Docker summary should parse tab-separated instructions and line continuations."""
    (tmp_path / "Dockerfile").write_text(
        "\n".join(
            [
                "FROM\tpython:3.12-slim",
                'ENTRYPOINT ["python", \\',
                '  "-m", "app.main"]',
                'CMD\t["--serve"]',
            ]
        ),
        encoding="utf-8",
    )

    summary = prompt._summarize_dockerfile(tmp_path)

    assert summary is not None
    assert "base=python:3.12-slim" in summary
    assert 'entrypoint=["python", "-m", "app.main"]' in summary
    assert 'cmd=["--serve"]' in summary


def test_summarize_dockerfile_resets_entrypoint_and_cmd_between_stages(tmp_path: Path) -> None:
    """ENTRYPOINT and CMD should reset when Dockerfile moves to a new FROM stage."""
    (tmp_path / "Dockerfile").write_text(
        "\n".join(
            [
                "FROM python:3.12 as build",
                'ENTRYPOINT ["python", "build.py"]',
                'CMD ["build"]',
                "FROM gcr.io/distroless/python3",
                "RUN echo ready",
            ]
        ),
        encoding="utf-8",
    )

    summary = prompt._summarize_dockerfile(tmp_path)

    assert summary is not None
    assert "base=gcr.io/distroless/python3" in summary
    assert "entrypoint=" not in summary
    assert "cmd=" not in summary


def test_combine_dockerfile_logical_lines_skips_comment_lines_between_continuations() -> None:
    """Comment lines should not break a continued Dockerfile instruction."""
    logical = prompt._combine_dockerfile_logical_lines(
        "\n".join(
            [
                "RUN echo one \\",
                "  && echo two \\",
                "  # ignored comment",
                "  && echo three",
            ]
        )
    )

    assert logical == ["RUN echo one && echo two && echo three"]


def test_build_build_test_section_handles_non_positive_budget(tmp_path: Path) -> None:
    """Non-positive character budgets should suppress build/test context rendering."""
    (tmp_path / "package.json").write_text('{"scripts":{"test":"pytest -q"}}', encoding="utf-8")

    assert build_build_test_section(tmp_path, max_characters=0) is None


def test_build_build_test_section_truncates_long_output(tmp_path: Path) -> None:
    """Build/test section should obey max-character truncation when needed."""
    (tmp_path / "package.json").write_text(
        "\n".join(
            [
                "{",
                '  "scripts": {',
                '    "test": "pytest -q",',
                '    "lint": "ruff check .",',
                '    "type": "mypy .",',
                '    "format": "ruff format ."',
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )

    section = build_build_test_section(tmp_path, max_characters=40)

    assert section is not None
    assert len(section) <= 40
    assert section.endswith("... (truncated)")


def test_build_available_tools_section_handles_empty_tool_list() -> None:
    """Empty tool definitions should render the configured fallback message."""

    class EmptyToolClient:
        def list_tool_definitions(self) -> list[object]:
            return []

    section = prompt.build_available_tools_section(tool_client=EmptyToolClient())  # type: ignore[arg-type]

    assert section == "## Available Tools\n- No tools configured."


def test_read_workspace_agents_guidance_missing_and_short_paths(tmp_path: Path) -> None:
    """AGENTS guidance should return None when absent and full text when within budget."""
    assert prompt.read_workspace_agents_guidance(tmp_path) is None

    (tmp_path / "AGENTS.md").write_text("Use minimal diffs.\n", encoding="utf-8")
    assert (
        prompt.read_workspace_agents_guidance(tmp_path, max_characters=80) == "Use minimal diffs."
    )


def test_read_workspace_repo_guidance_handles_zero_budget_and_agents_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repo guidance should tolerate zero budget and AGENTS read failures."""
    assert prompt.read_workspace_repo_guidance(tmp_path, max_characters=0) == (None, None)

    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text("Do not run destructive commands.\n", encoding="utf-8")
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "tiny.md").write_text("# Tiny skill\n", encoding="utf-8")

    original_read_text_prefix = prompt._read_text_prefix

    def fake_read_text_prefix(path: Path, *, max_characters: int) -> str:
        if path == agents_path:
            raise OSError("denied")
        return original_read_text_prefix(path, max_characters=max_characters)

    monkeypatch.setattr(prompt, "_read_text_prefix", fake_read_text_prefix)

    agents_guidance, assets_guidance = prompt.read_workspace_repo_guidance(
        tmp_path, max_characters=120
    )
    assert agents_guidance is None
    assert assets_guidance is not None
    assert "skills/tiny.md" in assets_guidance


def test_build_workspace_directory_listing_handles_missing_file_and_empty_workspace(
    tmp_path: Path,
) -> None:
    """Listing should handle missing paths, non-directories, and empty directories."""
    assert (
        prompt.build_workspace_directory_listing(tmp_path / "missing")
        == "<workspace path does not exist>"
    )

    single_file = tmp_path / "README.md"
    single_file.write_text("# Demo\n", encoding="utf-8")
    assert (
        prompt.build_workspace_directory_listing(single_file)
        == "<workspace path is not a directory>"
    )

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert prompt.build_workspace_directory_listing(empty_dir) == "<workspace is empty>"


def test_yaml_helpers_handle_comments_quotes_and_invalid_values() -> None:
    """YAML helper parsing should preserve quoted fragments and drop invalid identifiers."""
    assert prompt._strip_yaml_comment("value # comment") == "value "
    assert prompt._strip_yaml_comment('"value # keep" # drop') == '"value # keep" '
    assert (
        prompt._strip_yaml_comment("https://example.com/docs#anchor")
        == "https://example.com/docs#anchor"
    )
    assert prompt._split_inline_yaml_list_values("\"a,b\", c, 'd,e'") == ['"a,b"', "c", "'d,e'"]

    assert prompt._parse_inline_yaml_key_values("workflow_dispatch") == ["workflow_dispatch"]
    assert prompt._parse_inline_yaml_key_values('["bad$key", "push"]') == ["push"]


def test_summarize_package_scripts_and_workflows_handle_invalid_payloads(tmp_path: Path) -> None:
    """Config summaries should skip malformed payloads and unsupported workflow files."""
    (tmp_path / "package.json").write_text("[]", encoding="utf-8")
    assert prompt._summarize_package_scripts(tmp_path) is None

    (tmp_path / "package.json").write_text(
        '{"scripts":{"ok":"pytest -q","bad":5}}',
        encoding="utf-8",
    )
    scripts_summary = prompt._summarize_package_scripts(tmp_path)
    assert scripts_summary is not None
    assert 'ok="pytest -q"' in scripts_summary
    assert "bad" not in scripts_summary

    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / "ignored.yml").write_text("name: ci\n", encoding="utf-8")
    (workflows_dir / "valid.yaml").write_text("on: [push]\njobs:\n  test:\n", encoding="utf-8")

    workflow_summaries = prompt._summarize_github_workflows(tmp_path)
    assert len(workflow_summaries) == 1
    assert "valid.yaml" in workflow_summaries[0]


def test_summarize_package_scripts_prioritizes_common_actionable_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Common script names should be prioritized when summary item cap is small."""
    monkeypatch.setattr(prompt, "_BUILD_CONTEXT_ITEM_LIMIT", 3)
    scripts_payload = {
        "scripts": {
            "a": "echo a",
            "z": "echo z",
            "build": "npm run build",
            "test": "pytest -q",
            "lint": "ruff check .",
        }
    }
    (tmp_path / "package.json").write_text(json.dumps(scripts_payload), encoding="utf-8")

    scripts_summary = prompt._summarize_package_scripts(tmp_path)

    assert scripts_summary is not None
    assert "build=" in scripts_summary
    assert "lint=" in scripts_summary
    assert "test=" in scripts_summary
    assert "a=" not in scripts_summary
    assert "z=" not in scripts_summary


def test_summarize_dockerfile_uses_build_context_value_budget_for_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker summary command truncation should use build-context value character budget."""
    monkeypatch.setattr(prompt, "_BUILD_CONTEXT_VALUE_MAX_CHARACTERS", 120)
    long_payload = "x" * 100
    (tmp_path / "Dockerfile").write_text(
        "\n".join(
            [
                "FROM python:3.12",
                f"ENTRYPOINT {long_payload}",
                f"CMD {long_payload}",
            ]
        ),
        encoding="utf-8",
    )

    summary = prompt._summarize_dockerfile(tmp_path)

    assert summary is not None
    assert "... (truncated)" not in summary


def test_summaries_truncate_large_package_scripts_and_workflow_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Package scripts and workflow on/jobs fields should stay bounded per rendered value."""
    monkeypatch.setattr(prompt, "_BUILD_CONTEXT_VALUE_MAX_CHARACTERS", 30)
    long_command = "python -m pytest " + "x" * 200
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": long_command}}),
        encoding="utf-8",
    )

    scripts_summary = prompt._summarize_package_scripts(tmp_path)
    assert scripts_summary is not None
    assert "... (truncated)" in scripts_summary

    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / "large.yml").write_text(
        "\n".join(
            [
                (
                    "on: [push, pull_request, workflow_dispatch, "
                    "schedule, repository_dispatch, workflow_run]"
                ),
                "jobs:",
                "  test:",
                "  lint:",
                "  package:",
                "  publish:",
                "  smoke:",
            ]
        ),
        encoding="utf-8",
    )
    workflow_summaries = prompt._summarize_github_workflows(tmp_path)
    assert len(workflow_summaries) == 1
    assert "... (truncated)" in workflow_summaries[0]


def test_normalize_command_hint_and_contributing_summary_without_hints(tmp_path: Path) -> None:
    """Command normalization should filter noise and contributing summary may be absent."""
    assert prompt._normalize_command_hint("") is None
    assert prompt._normalize_command_hint("`echo hello`") is None
    assert prompt._normalize_command_hint("`pytest -q`") == "pytest -q"

    (tmp_path / "CONTRIBUTING.md").write_text("Read the guide.\n", encoding="utf-8")
    assert prompt._summarize_contributing_commands(tmp_path) is None


def test_normalize_command_hint_supports_common_non_python_toolchains() -> None:
    """Command normalization should recognize actionable hints across common language stacks."""
    assert prompt._normalize_command_hint("go test ./...") == "go test ./..."
    assert prompt._normalize_command_hint("cargo test --all") == "cargo test --all"
    assert prompt._normalize_command_hint("composer test") == "composer test"
    assert prompt._normalize_command_hint("mvn test") == "mvn test"
    assert prompt._normalize_command_hint("gradle test") == "gradle test"
    assert prompt._normalize_command_hint("rake spec") == "rake spec"
    assert prompt._normalize_command_hint("bundle exec rspec") == "bundle exec rspec"
    assert prompt._normalize_command_hint("dotnet test") == "dotnet test"


def test_combine_dockerfile_lines_json_safe_and_mask_repo_url_edges() -> None:
    """Helper utilities should handle trailing continuations and masking edge cases."""
    logical = prompt._combine_dockerfile_logical_lines("RUN echo hi \\\n  && echo there \\\n")
    assert logical == ["RUN echo hi && echo there"]

    normalized = prompt._json_safe({"path": Path("demo"), "items": {"z", 2}})
    assert isinstance(normalized, dict)
    assert normalized["path"] == "demo"

    assert prompt._mask_repo_url(None) is None
    assert prompt._mask_repo_url("github.com/example/repo.git") == "github.com/example/repo.git"
    assert prompt._mask_repo_url("ssh://@example.com/repo.git") == "ssh://@example.com/repo.git"
    assert (
        prompt._mask_repo_url("https://user:token@example.com/repo.git")
        == "https://***@example.com/repo.git"
    )


def test_combine_dockerfile_logical_lines_handles_inline_comment_after_backslash() -> None:
    """Continuation detection should work when a trailing comment follows the backslash."""
    logical = prompt._combine_dockerfile_logical_lines(
        "\n".join(
            [
                "RUN echo one \\ # continue",
                "  && echo two",
            ]
        )
    )

    assert logical == ["RUN echo one && echo two"]


def test_combine_dockerfile_logical_lines_ignores_even_trailing_backslashes() -> None:
    """An even number of trailing backslashes should not be treated as continuation."""
    logical = prompt._combine_dockerfile_logical_lines(
        "\n".join(
            [
                "RUN echo one \\\\",
                "RUN echo two",
            ]
        )
    )

    assert logical == ["RUN echo one \\\\", "RUN echo two"]
