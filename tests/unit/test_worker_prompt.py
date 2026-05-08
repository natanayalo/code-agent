"""Unit tests for worker system prompt construction."""

from __future__ import annotations

from workers.base import WorkerRequest
from workers.prompt import build_system_prompt


def test_build_system_prompt_respects_delivery_mode_summary(tmp_path) -> None:
    """System prompt should use analysis-focused role for summary delivery mode."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Analyze the codebase",
        repo_url="https://example.com/repo.git",
        task_spec={"delivery_mode": "summary"},
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "coding execution worker" in prompt
    assert "read permissions" in prompt
    assert "analyze files" in prompt
    assert "Do not modify files." in prompt
    assert "Prefer minimal edits" not in prompt


def test_build_system_prompt_respects_delivery_mode_workspace(tmp_path) -> None:
    """System prompt should use analysis-focused role."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Fix the bug",
        repo_url="https://example.com/repo.git",
        task_spec={"delivery_mode": "workspace"},
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "coding execution worker" in prompt
    assert "read/write permissions" in prompt
    assert "make smallest safe changes" in prompt
    assert "Do not modify any files" not in prompt
    assert "Prefer minimal edits" in prompt


def test_build_system_prompt_omits_verbose_json_bloats(tmp_path) -> None:
    """System prompt should extract key fields instead of dumping raw JSON blobs."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Implement feature",
        repo_url="https://example.com/repo.git",
        task_spec={
            "goal": "Implement feature",
            "assumptions": ["Assume X"],
            "acceptance_criteria": ["Done when Y"],
            "delivery_mode": "workspace",
        },
        memory_context={"large": "context"},
        task_plan={"steps": ["step1"]},
        constraints={"risk_level": "low", "some_internal_junk": "junk"},
    )

    prompt = build_system_prompt(request, tmp_path)

    # Key fields should be present as markdown
    assert "Assumptions:" in prompt
    assert "- Assume X" in prompt
    assert "Acceptance criteria:" in prompt
    assert "- Done when Y" in prompt

    # Large/internal JSON blobs should be omitted
    assert '"large": "context"' not in prompt
    assert '"steps": ["step1"]' not in prompt
    assert '"some_internal_junk": "junk"' not in prompt

    # Filtered constraints should be present
    assert '"risk_level": "low"' in prompt
