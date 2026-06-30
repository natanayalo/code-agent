"""Tests for GitHub repository helper utilities."""

from __future__ import annotations

from orchestrator.github_repo import github_repo_spec_from_url


def test_github_repo_spec_from_common_clone_urls() -> None:
    """GitHub clone URLs should normalize to gh CLI -R values."""
    assert (
        github_repo_spec_from_url("https://github.com/natanayalo/code-agent.git")
        == "natanayalo/code-agent"
    )
    assert (
        github_repo_spec_from_url("git@github.com:natanayalo/code-agent.git")
        == "github.com/natanayalo/code-agent"
    )
    assert github_repo_spec_from_url("natanayalo/code-agent") == "natanayalo/code-agent"
    assert github_repo_spec_from_url("") is None
