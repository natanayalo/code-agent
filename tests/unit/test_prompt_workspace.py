"""Tests for bounded workspace guidance used in worker prompts."""

from __future__ import annotations

from workers.prompt_workspace import (
    _extract_front_matter_metadata,
    _first_meaningful_line,
    _read_text_prefix,
    _truncate_to_budget,
    build_repo_context_section,
    build_workspace_directory_listing,
    read_workspace_agents_assets_guidance,
    read_workspace_agents_guidance,
    read_workspace_repo_guidance,
)


def test_workspace_guidance_helpers_handle_malformed_and_tiny_content() -> None:
    """Prompt-boundary helpers keep malformed metadata and tiny budgets harmless."""
    assert _truncate_to_budget("value", max_characters=0) == ""
    assert _truncate_to_budget("value", max_characters=3) == "\n.."
    assert _truncate_to_budget("value", max_characters=10) == "value"
    assert _extract_front_matter_metadata("---\ninvalid\nname:\n---\nbody") == (
        None,
        None,
        "body",
    )
    assert _extract_front_matter_metadata("---\nname: unfinished") == (
        None,
        None,
        "---\nname: unfinished",
    )
    assert _first_meaningful_line("\n#\n  useful content\n") == "useful content"
    assert _first_meaningful_line("\n#\n") is None


def test_read_workspace_agents_guidance_returns_none_and_truncates(tmp_path) -> None:
    """Root guidance is optional and must remain within its prompt budget."""
    assert read_workspace_agents_guidance(tmp_path) is None

    (tmp_path / "AGENTS.md").write_text("abcdef", encoding="utf-8")

    assert read_workspace_agents_guidance(tmp_path, max_characters=4) == "abcd\n... (truncated)"


def test_read_workspace_agents_assets_guidance_summarizes_front_matter(tmp_path) -> None:
    """Markdown assets expose metadata without including their entire body."""
    skill_path = tmp_path / ".agents" / "skills" / "deploy" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: Safe deploy\ndescription: Deploy with a rollback plan\n---\n# Details\n",
        encoding="utf-8",
    )
    workflow_path = tmp_path / ".agents" / "workflows" / "review.md"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text("# Review\nInspect the change.\n", encoding="utf-8")

    guidance = read_workspace_agents_assets_guidance(tmp_path, max_characters=500)

    assert guidance == (
        "- skills/deploy/SKILL.md: Safe deploy - Deploy with a rollback plan\n"
        "- workflows/review.md: review - Review"
    )


def test_read_workspace_agents_assets_guidance_honors_empty_and_tight_budgets(tmp_path) -> None:
    """Asset summaries are omitted or truncated predictably when prompt space is scarce."""
    asset_path = tmp_path / ".agents" / "agents" / "helper.md"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_text("# Helper\nUseful details\n", encoding="utf-8")

    assert read_workspace_agents_assets_guidance(tmp_path, max_characters=0) is None
    assert read_workspace_agents_assets_guidance(tmp_path, max_characters=12) == "\n... (trunca"


def test_workspace_assets_and_context_handle_absent_guidance(tmp_path) -> None:
    """No guidance files still produces a valid repository context section."""
    assert read_workspace_agents_assets_guidance(tmp_path, max_characters=50) is None
    assert read_workspace_repo_guidance(tmp_path, max_characters=0) == (None, None)
    assert build_repo_context_section(tmp_path) == (
        "## Repo Context\nDirectory listing:\n```text\n<workspace is empty>\n```"
    )


def test_workspace_assets_support_empty_folders_and_summary_only_files(tmp_path) -> None:
    """Empty asset roots and metadata-only files remain safe prompt inputs."""
    agents_root = tmp_path / ".agents"
    agents_root.mkdir()
    assert read_workspace_agents_assets_guidance(tmp_path, max_characters=50) is None

    asset_path = agents_root / "skills" / "metadata.md"
    asset_path.parent.mkdir()
    asset_path.write_text("---\n---\n", encoding="utf-8")

    assert (
        read_workspace_agents_assets_guidance(tmp_path, max_characters=50)
        == "- skills/metadata.md: metadata"
    )
    assert _read_text_prefix(asset_path, max_characters=0) == ""


def test_workspace_assets_skip_unreadable_files(tmp_path, monkeypatch) -> None:
    """An unreadable optional asset must not prevent construction of a worker prompt."""
    asset_path = tmp_path / ".agents" / "skills" / "unreadable.md"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_text("# Unreadable\n", encoding="utf-8")

    def _raise_os_error(*_args, **_kwargs) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr("workers.prompt_workspace._read_text_prefix", _raise_os_error)

    assert read_workspace_agents_assets_guidance(tmp_path, max_characters=50) is None


def test_read_workspace_repo_guidance_shares_budget_with_assets(tmp_path) -> None:
    """Root AGENTS.md consumes the shared budget before .agents summaries."""
    (tmp_path / "AGENTS.md").write_text("root policy", encoding="utf-8")
    asset_path = tmp_path / ".agents" / "skills" / "helper.md"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_text("# Helper\nUseful details\n", encoding="utf-8")

    root_guidance, assets_guidance = read_workspace_repo_guidance(tmp_path, max_characters=11)

    assert root_guidance == "root policy"
    assert assets_guidance is None


def test_build_workspace_directory_listing_filters_and_bounds_entries(tmp_path) -> None:
    """Directory context is deterministic, ignores runtime folders, and has a hard cap."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("", encoding="utf-8")
    (tmp_path / "README.md").write_text("", encoding="utf-8")

    assert build_workspace_directory_listing(tmp_path, max_depth=2) == "src/\nsrc/app.py\nREADME.md"
    assert (
        build_workspace_directory_listing(tmp_path, max_entries=2)
        == "src/\nsrc/app.py\n... (truncated)"
    )


def test_build_workspace_directory_listing_handles_missing_file_and_empty_paths(tmp_path) -> None:
    """Listing feedback distinguishes invalid workspaces from valid empty directories."""
    assert build_workspace_directory_listing(tmp_path) == "<workspace is empty>"
    assert (
        build_workspace_directory_listing(tmp_path / "missing") == "<workspace path does not exist>"
    )

    file_path = tmp_path / "file.txt"
    file_path.write_text("not a directory", encoding="utf-8")

    assert build_workspace_directory_listing(file_path) == "<workspace path is not a directory>"


def test_build_workspace_directory_listing_stops_at_max_depth(tmp_path) -> None:
    """Nested children outside the depth budget are omitted from prompt context."""
    nested_directory = tmp_path / "top" / "nested"
    nested_directory.mkdir(parents=True)
    (nested_directory / "hidden.py").write_text("", encoding="utf-8")

    assert build_workspace_directory_listing(tmp_path, max_depth=1) == "top/"
