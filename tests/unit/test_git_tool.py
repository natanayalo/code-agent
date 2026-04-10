"""Unit tests for the structured git helper wrapper."""

from __future__ import annotations

import pytest

from tools import GitToolError, build_git_command_from_input


def test_build_git_command_from_input_supports_status() -> None:
    """Status requests should render deterministic git status commands."""
    command = build_git_command_from_input(
        '{"operation":"status","porcelain":true,"include_untracked":false}'
    )

    assert command == "git status --porcelain=v1 --untracked-files=no"


def test_build_git_command_from_input_supports_diff_with_pathspecs() -> None:
    """Diff requests should preserve optional revision and pathspec filters."""
    command = build_git_command_from_input(
        '{"operation":"diff","staged":true,"against":"HEAD~1","pathspecs":["tools","tests"]}'
    )

    assert command == "git diff --cached 'HEAD~1' -- tools tests"


def test_build_git_command_from_input_supports_branch_create() -> None:
    """Branch requests should support safe branch creation helpers."""
    command = build_git_command_from_input(
        '{"operation":"branch","create":true,"branch_name":"task/t-080-git-wrapper"}'
    )

    assert command == "git branch -- task/t-080-git-wrapper"


def test_build_git_command_from_input_supports_commit_messages() -> None:
    """Commit requests should shell-escape messages safely."""
    command = build_git_command_from_input(
        '{"operation":"commit","include_all":true,"message":"feat: add git wrapper"}'
    )

    assert command == "git commit -a -m 'feat: add git wrapper'"


def test_build_git_command_from_input_rejects_invalid_json() -> None:
    """The git helper should fail clearly when the runtime provides invalid JSON."""
    with pytest.raises(GitToolError, match="valid JSON"):
        build_git_command_from_input("status")


def test_build_git_command_from_input_rejects_invalid_field_combinations() -> None:
    """Operation-specific fields should stay constrained to the supported helper shape."""
    with pytest.raises(GitToolError, match="do not support `message`"):
        build_git_command_from_input('{"operation":"status","message":"should fail"}')


def test_build_git_command_from_input_rejects_branch_name_without_create() -> None:
    """Branch list/show helpers should reject branch_name when create is false."""
    with pytest.raises(GitToolError, match="do not support `branch_name`"):
        build_git_command_from_input('{"operation":"branch","branch_name":"topic/extra"}')
