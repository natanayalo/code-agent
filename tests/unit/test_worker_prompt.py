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
        constraints={"read_only": True},
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
        constraints={"read_only": False},
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


def test_build_system_prompt_redacts_private_tagged_sections(tmp_path) -> None:
    """Worker prompts should never include user-marked private blocks."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Fix <private>secret task detail</private>",
        repo_url="https://example.com/repo.git",
        task_spec={
            "acceptance_criteria": ["Do not reveal <private>secret criterion</private>"],
        },
        memory_context={
            "personal": [
                {
                    "memory_key": "style",
                    "value": {"note": "<private>secret memory</private>"},
                }
            ],
            "observations": [
                {
                    "id": "obs-1",
                    "observed_at": "2026-07-04T12:00:00Z",
                    "source": "worker",
                    "event_type": "worker_completed",
                    "summary": "Saw <private>secret observation</private>",
                }
            ],
        },
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "secret task detail" not in prompt
    assert "secret criterion" not in prompt
    assert "secret memory" not in prompt
    assert "secret observation" not in prompt
    assert prompt.count("[redacted-private]") == 4


def test_build_system_prompt_filters_tools_by_permission(tmp_path) -> None:
    """System prompt should only list tools permitted by the current constraints."""
    from tools import DEFAULT_TOOL_REGISTRY, EXECUTE_BASH_TOOL_NAME, VIEW_FILE_TOOL_NAME

    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")

    # Read-only request
    request_ro = WorkerRequest(
        task_text="Check status",
        repo_url="https://example.com/repo.git",
        constraints={"read_only": True},
    )
    prompt_ro = build_system_prompt(request_ro, tmp_path, tool_registry=DEFAULT_TOOL_REGISTRY)

    assert f"### `{VIEW_FILE_TOOL_NAME}`" in prompt_ro
    assert f"### `{EXECUTE_BASH_TOOL_NAME}`" not in prompt_ro

    # Write request
    request_rw = WorkerRequest(
        task_text="Fix bug",
        repo_url="https://example.com/repo.git",
        constraints={"read_only": False},
    )
    prompt_rw = build_system_prompt(request_rw, tmp_path, tool_registry=DEFAULT_TOOL_REGISTRY)

    assert f"### `{VIEW_FILE_TOOL_NAME}`" in prompt_rw
    assert f"### `{EXECUTE_BASH_TOOL_NAME}`" in prompt_rw


def test_build_system_prompt_honors_explicit_requested_tool_subset(tmp_path) -> None:
    from tools import DEFAULT_TOOL_REGISTRY, VIEW_FILE_TOOL_NAME

    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Inspect files only",
        repo_url="https://example.com/repo.git",
        constraints={"read_only": False},
        tools=[VIEW_FILE_TOOL_NAME],
    )
    prompt = build_system_prompt(request, tmp_path, tool_registry=DEFAULT_TOOL_REGISTRY)
    assert f"### `{VIEW_FILE_TOOL_NAME}`" in prompt
    assert "execute_bash" not in prompt


def test_build_system_prompt_includes_repo_scout_overlay(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Inspect debt",
        repo_url="https://example.com/repo.git",
        constraints={
            "task_type": "scout",
            "scout_mode": "repo",
            "max_proposals": 5,
        },
    )
    prompt = build_system_prompt(request, tmp_path)
    assert "## Scout Mode Guardrails" in prompt
    assert "up to 5 structured proposal(s)" in prompt
    assert "Return exactly one JSON object" in prompt
    assert "`implementation_slice`" in prompt
    assert "Mode: `repo`" in prompt
    assert "Focus on inspecting the local repository" in prompt


def test_build_system_prompt_includes_research_scout_overlay(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Research DB patterns",
        repo_url="https://example.com/repo.git",
        constraints={
            "task_type": "scout",
            "scout_mode": "research",
            "scout_focus": "SQLAlchemy migrations",
            "scout_depth": "deep",
            "max_proposals": 2,
        },
    )
    prompt = build_system_prompt(request, tmp_path)
    assert "## Scout Mode Guardrails" in prompt
    assert "up to 2 structured proposal(s)" in prompt
    assert "Mode: `research`" in prompt
    assert "Depth: `deep`" in prompt
    assert "Focus: SQLAlchemy migrations" in prompt
    assert "Pay close attention to the requested focus area" in prompt
    assert "Source Policy: use available/local evidence first" in prompt


def test_build_system_prompt_includes_deep_scout_overlay(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Deep dive on architecture",
        repo_url="https://example.com/repo.git",
        constraints={
            "task_type": "scout",
            "scout_mode": "deep",
        },
    )
    prompt = build_system_prompt(request, tmp_path)
    assert "## Scout Mode Guardrails" in prompt
    assert "up to 3 structured proposal(s)" in prompt
    assert "Mode: `deep`" in prompt
    assert "Use a repo-first, then targeted-research structure" in prompt
    assert "Source Policy: use available/local evidence first" in prompt


def test_build_system_prompt_omits_scout_overlay_for_normal_tasks(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Fix bug",
        repo_url="https://example.com/repo.git",
        constraints={
            "task_type": "feature",
            "scout_mode": "repo",  # Should be ignored if not scout
        },
    )
    prompt = build_system_prompt(request, tmp_path)
    assert "Scout Mode Guardrails" not in prompt


def test_build_system_prompt_ignores_non_string_scout_constraints(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Research DB patterns",
        repo_url="https://example.com/repo.git",
        constraints={
            "task_type": "scout",
            "scout_mode": "research",
            "scout_focus": ["not", "a", "string"],
            "scout_depth": {"invalid": "type"},
            "max_proposals": 2,
        },
    )
    prompt = build_system_prompt(request, tmp_path)
    assert "## Scout Mode Guardrails" in prompt
    assert "Mode: `research`" in prompt
    # Since depth and focus are invalid, they should be omitted
    assert "Depth:" not in prompt
    assert "Focus:" not in prompt
