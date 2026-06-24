"""Unit tests for structured worker prompt assembly."""

from __future__ import annotations

from pathlib import Path

import workers.prompt as prompt
from workers.base import WorkerRequest
from workers.prompt import (
    build_build_test_section,
    build_runtime_manifest_section,
    build_system_prompt,
    build_task_context_section,
)
from workers.prompt_review import build_review_prompt


def test_build_system_prompt_includes_all_expected_sections(tmp_path: Path) -> None:
    """The prompt should assemble role, tools, repo, task, and workflow sections."""
    (tmp_path / "AGENTS.md").write_text("Prefer small diffs.\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")

    request = WorkerRequest(
        session_id="session-46",
        repo_url="https://token@github.com/example/repo.git",
        branch="main",
        task_text="Build the structured system prompt module",
    )

    rendered = build_system_prompt(request, tmp_path)

    assert "## Role" in rendered
    assert "## Repo Context" in rendered
    assert "## Task Context" in rendered
    assert "## Workflow Instructions" in rendered
    assert "Your first action MUST be to read `AGENTS.md`" in rendered
    assert "README.md" in rendered


def test_runtime_manifest_section_renders_operating_contract() -> None:
    """Runtime manifest context should be visible when provided."""
    request = WorkerRequest(
        task_text="Run safely",
        runtime_manifest={
            "service": {"service_name": "code-agent", "schema_version": 1},
            "task": {
                "read_only": True,
                "forbidden_actions": ["hardcode_secrets"],
            },
            "maintenance_actions": [
                {"action": "operator_attention", "request_only": True},
            ],
        },
    )

    section = build_runtime_manifest_section(request)

    assert "## Runtime Operating Contract" in section
    assert '"read_only": true' in section
    assert "hardcode_secrets" in section
    assert "request-only maintenance actions" in section


def test_runtime_manifest_section_omits_blank_contract() -> None:
    """Prompt rendering should stay unchanged when no runtime manifest exists."""
    assert build_runtime_manifest_section(WorkerRequest(task_text="Run safely")) == ""


def test_build_review_prompt_includes_guidance(tmp_path: Path) -> None:
    """Review-mode prompt should include repo guidance and optional REVIEW.md context."""
    (tmp_path / "AGENTS.md").write_text("Keep changes small.\n", encoding="utf-8")
    (tmp_path / "REVIEW.md").write_text("Flag only concrete regressions.\n", encoding="utf-8")

    rendered = build_review_prompt(
        workspace_path=tmp_path,
        review_context_packet="Packet payload",
        reviewer_kind="independent_reviewer",
        task_text="Review updated auth validation",
    )

    assert "## Review Role" in rendered
    assert "## Review Guidance" in rendered
    assert "AGENTS.md guidance:" in rendered
    assert "REVIEW.md guidance:" in rendered
    assert "## Review Task" in rendered
    assert "Task objective: Review updated auth validation" in rendered


def test_build_build_test_section_detects_files(tmp_path: Path) -> None:
    """Build/test section should detect presence of common config files."""
    (tmp_path / "pyproject.toml").write_text("[project]", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM scratch", encoding="utf-8")

    section = build_build_test_section(tmp_path)
    assert "## Build & Test" in section
    assert "pyproject.toml" in section
    assert "Dockerfile" in section


def test_build_task_context_section_basic() -> None:
    """Task context should show the goal."""
    request = WorkerRequest(task_text="Check the logs")
    section = build_task_context_section(request)
    assert "Goal: Check the logs" in section


def test_json_safe_and_mask_repo_url_edges() -> None:
    """Helper utilities should handle masking and JSON normalization edge cases."""
    normalized = prompt._json_safe({"path": Path("demo"), "items": {"z", 2}})
    assert isinstance(normalized, dict)
    assert normalized["path"] == "demo"
    assert set(normalized["items"]) == {2, "z"}

    assert prompt._mask_repo_url(None) is None
    assert (
        prompt._mask_repo_url("https://user:token@example.com/repo.git")
        == "https://***@example.com/repo.git"
    )
