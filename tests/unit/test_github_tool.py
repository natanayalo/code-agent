"""Unit tests for the structured GitHub helper wrapper."""

from __future__ import annotations

import pytest

from tools import GitHubToolError, build_github_command_from_input


def test_build_github_command_from_input_supports_pr_create_draft() -> None:
    """Draft PR requests should render deterministic gh pr create commands."""
    command = build_github_command_from_input(
        '{"operation":"pr_create_draft","repository_full_name":"openai/code-agent",'
        '"base_branch":"master","head_branch":"task/t-081","title":"feat: add wrapper",'
        '"body":"Adds the GitHub wrapper."}'
    )

    assert command == (
        "gh pr create --draft --repo openai/code-agent --base master --head task/t-081 "
        "--title 'feat: add wrapper' --body 'Adds the GitHub wrapper.'"
    )


def test_build_github_command_from_input_supports_pr_comment() -> None:
    """PR comment requests should render deterministic gh pr comment commands."""
    command = build_github_command_from_input(
        '{"operation":"pr_comment","repository_full_name":"openai/code-agent",'
        '"pr_number":59,"comment_body":"Looks good."}'
    )

    assert command == "gh pr comment 59 --repo openai/code-agent --body 'Looks good.'"


def test_build_github_command_from_input_rejects_invalid_json() -> None:
    """The GitHub helper should fail clearly when runtime input is invalid JSON."""
    with pytest.raises(GitHubToolError, match="valid JSON"):
        build_github_command_from_input("pr_comment")


def test_build_github_command_from_input_rejects_invalid_repository_shape() -> None:
    """GitHub helper requests should require owner/name repository identifiers."""
    with pytest.raises(GitHubToolError, match="owner/name"):
        build_github_command_from_input(
            '{"operation":"pr_comment","repository_full_name":"not-valid",'
            '"pr_number":59,"comment_body":"Looks good."}'
        )


def test_build_github_command_from_input_rejects_hyphen_prefixed_branches() -> None:
    """Draft PR creation should reject branch names that can be mistaken for flags."""
    with pytest.raises(GitHubToolError, match="cannot start with a hyphen"):
        build_github_command_from_input(
            '{"operation":"pr_create_draft","repository_full_name":"openai/code-agent",'
            '"base_branch":"master","head_branch":"-bad","title":"t","body":"b"}'
        )


def test_build_github_command_from_input_rejects_pr_create_only_fields_on_comment() -> None:
    """Comment requests should reject draft-PR-only fields."""
    with pytest.raises(GitHubToolError, match="do not support `title`"):
        build_github_command_from_input(
            '{"operation":"pr_comment","repository_full_name":"openai/code-agent",'
            '"pr_number":59,"comment_body":"Looks good.","title":"unexpected"}'
        )
