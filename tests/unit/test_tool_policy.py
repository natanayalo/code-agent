"""Unit tests for shell permission-policy helpers."""

from __future__ import annotations

from tools import (
    DEFAULT_TOOL_REGISTRY,
    ToolPermissionLevel,
    granted_permission_from_constraints,
    permission_allows,
    resolve_bash_command_permission,
)


def test_granted_permission_from_constraints_defaults_to_workspace_write() -> None:
    """Worker runs should default to workspace-write authority when nothing narrower is given."""
    assert granted_permission_from_constraints({}) == ToolPermissionLevel.WORKSPACE_WRITE


def test_granted_permission_from_constraints_parses_known_strings() -> None:
    """Constraint values should accept the explicit permission strings we expose."""
    resolved = granted_permission_from_constraints({"granted_permission": "dangerous_shell"})

    assert resolved == ToolPermissionLevel.DANGEROUS_SHELL


def test_permission_allows_respects_permission_ordering() -> None:
    """Higher permission levels should satisfy lower-level requests."""
    assert permission_allows(
        ToolPermissionLevel.DANGEROUS_SHELL,
        ToolPermissionLevel.WORKSPACE_WRITE,
    )
    assert not permission_allows(
        ToolPermissionLevel.WORKSPACE_WRITE,
        ToolPermissionLevel.NETWORKED_WRITE,
    )


def test_resolve_bash_command_permission_classifies_read_only_commands() -> None:
    """Read-only shell commands should not require workspace-write access."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "git status --short",
        tool,
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert decision.required_permission == ToolPermissionLevel.READ_ONLY
    assert decision.allowed is True


def test_resolve_bash_command_permission_does_not_treat_find_exec_as_read_only() -> None:
    """`find -exec` should fail closed instead of bypassing the permission ladder."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "find . -type f -exec touch marker {} +",
        tool,
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert decision.required_permission == ToolPermissionLevel.WORKSPACE_WRITE
    assert decision.allowed is False


def test_resolve_bash_command_permission_does_not_treat_redirection_as_read_only() -> None:
    """Shell redirection should keep otherwise safe commands out of the read-only tier."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "cat README.md > copy.txt",
        tool,
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert decision.required_permission == ToolPermissionLevel.WORKSPACE_WRITE
    assert decision.allowed is False


def test_safe_named_redirection_target_stays_out_of_read_only() -> None:
    """Write redirection should not become read-only just because the target looks harmless."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "cat secret > pwd",
        tool,
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert decision.required_permission == ToolPermissionLevel.WORKSPACE_WRITE
    assert decision.allowed is False


def test_resolve_bash_command_permission_does_not_treat_inline_redirection_as_read_only() -> None:
    """Inline redirection syntax should still fail closed without whitespace separators."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "cat README.md>copy.txt",
        tool,
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert decision.required_permission == ToolPermissionLevel.WORKSPACE_WRITE
    assert decision.allowed is False


def test_resolve_bash_command_permission_does_not_treat_rg_preprocessors_as_read_only() -> None:
    """Ripgrep commands that spawn preprocessors should not remain in the read-only tier."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "rg --pre 'touch marker' TODO",
        tool,
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert decision.required_permission == ToolPermissionLevel.WORKSPACE_WRITE
    assert decision.allowed is False


def test_resolve_bash_command_permission_classifies_quoted_rm_as_dangerous_shell() -> None:
    """Quoted executables should still be classified after shell token parsing."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        '"rm" -rf build',
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
    assert decision.allowed is False


def test_resolve_bash_command_permission_classifies_escaped_curl_as_networked_write() -> None:
    """Escaped executable text should still classify from the parsed command tokens."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        r"c\url https://example.com",
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.NETWORKED_WRITE
    assert decision.allowed is False


def test_resolve_bash_command_permission_classifies_wrapped_rm_as_dangerous_shell() -> None:
    """Known shell wrappers should not hide dangerous executables."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    for command in ("sudo rm -rf build", "env FOO=1 rm -rf build", "/bin/rm -rf build"):
        decision = resolve_bash_command_permission(
            command,
            tool,
            granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        )

        assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
        assert decision.allowed is False


def test_resolve_bash_command_permission_classifies_rmdir_and_eval_as_dangerous_shell() -> None:
    """Destructive directory removal and shell re-evaluation should require elevation."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    for command in ("rmdir build", 'eval "rm -rf build"'):
        decision = resolve_bash_command_permission(
            command,
            tool,
            granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        )

        assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
        assert decision.allowed is False


def test_resolve_bash_command_permission_classifies_source_builtins_as_dangerous_shell() -> None:
    """Shell built-ins that execute scripts in-process should require elevation."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    for command in ("source ./script.sh", ". ./script.sh"):
        decision = resolve_bash_command_permission(
            command,
            tool,
            granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        )

        assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
        assert decision.allowed is False


def test_resolve_bash_command_permission_classifies_shell_wrappers_as_dangerous_shell() -> None:
    """Nested shell execution should not bypass the permission ladder."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    for command in (
        "bash -c 'rm -rf build'",
        "sh -c 'rm -rf build'",
        "zsh -c 'rm -rf build'",
        "alias ll='rm -rf build'",
        "function ll(){ rm -rf build; }",
    ):
        decision = resolve_bash_command_permission(
            command,
            tool,
            granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        )

        assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
        assert decision.allowed is False


def test_classifies_inline_interpreters_as_dangerous_shell() -> None:
    """Inline interpreter evaluation should require elevated shell permission."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    for command in (
        "python -c 'import os; os.remove(\"x\")'",
        "python3 -c 'import os; os.remove(\"x\")'",
        'node -e \'require("fs").unlinkSync("x")\'',
        'node --eval \'require("fs").unlinkSync("x")\'',
        "perl -e 'unlink \"x\"'",
        "ruby -e 'File.delete(\"x\")'",
        "php -r 'unlink(\"x\");'",
    ):
        decision = resolve_bash_command_permission(
            command,
            tool,
            granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        )

        assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
        assert decision.allowed is False


def test_resolve_bash_command_permission_classifies_remote_tools_as_networked_write() -> None:
    """Remote execution and copy tools should require networked permission."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    for command in (
        "ssh host 'uptime'",
        "scp file host:/tmp/",
        "rsync -av . host:/tmp/x",
        "nc example.com 80",
    ):
        decision = resolve_bash_command_permission(
            command,
            tool,
            granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        )

        assert decision.required_permission == ToolPermissionLevel.NETWORKED_WRITE
        assert decision.allowed is False


def test_resolve_bash_command_permission_classifies_networked_git_commands() -> None:
    """Git commands that contact remotes should require networked permission."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    for command in ("git fetch origin", "git ls-remote origin"):
        decision = resolve_bash_command_permission(
            command,
            tool,
            granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        )

        assert decision.required_permission == ToolPermissionLevel.NETWORKED_WRITE
        assert decision.allowed is False


def test_resolve_bash_command_permission_does_not_treat_release_text_as_deploy() -> None:
    """Release-like argument text should not trip deploy classification."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        'git commit -m "release v1.0"',
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.WORKSPACE_WRITE
    assert decision.allowed is True


def test_resolve_bash_command_permission_does_not_treat_deploy_filenames_as_deploy() -> None:
    """Deploy-like filenames should not override the actual executable classification."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "cat deploy.log",
        tool,
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert decision.required_permission == ToolPermissionLevel.READ_ONLY
    assert decision.allowed is True


def test_resolve_bash_command_permission_classifies_chained_dangerous_shell_commands() -> None:
    """Later shell segments should still escalate the overall permission requirement."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "ls; rm -rf build",
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
    assert decision.allowed is False


def test_resolve_bash_command_permission_classifies_chained_network_commands() -> None:
    """Pipelines should inherit the highest required permission across segments."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "cat README.md | curl https://example.com",
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.NETWORKED_WRITE
    assert decision.allowed is False


def test_resolve_bash_command_permission_fails_closed_for_subshell_syntax() -> None:
    """Unsupported shell features should require elevated permission instead of failing open."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "$(rm -rf build)",
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
    assert decision.allowed is False


def test_resolve_bash_command_permission_fails_closed_for_grouping_subshells() -> None:
    """Grouping parens should share the unsupported-shell fail-closed path."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "(rm -rf build)",
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
    assert decision.allowed is False


def test_resolve_bash_command_permission_does_not_treat_quoted_parentheses_as_grouping() -> None:
    """Quoted arguments with parentheses should not trigger unsupported-shell detection."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        'git commit -m "fix (bug)"',
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.WORKSPACE_WRITE
    assert decision.allowed is True


def test_resolve_bash_command_permission_fails_closed_for_process_substitution() -> None:
    """Process substitution should share the unsupported-shell fail-closed path."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "echo hi <(rm -rf build)",
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
    assert decision.allowed is False


def test_fails_closed_for_variable_expansion_in_command_word() -> None:
    """Variable-expanded command words should fail closed instead of bypassing prefix checks."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    for command in ("${CMD} -rf build", "CMD=rm; $CMD -rf build"):
        decision = resolve_bash_command_permission(
            command,
            tool,
            granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        )

        assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
        assert decision.allowed is False


def test_resolve_bash_command_permission_fails_closed_for_globbed_command_word() -> None:
    """Command-word globbing should fail closed because it can hide the executable."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "r* -rf build",
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
    assert decision.allowed is False


def test_resolve_bash_command_permission_fails_closed_for_brace_expansion_in_command_word() -> None:
    """Brace-expanded command words should fail closed because they can hide executables."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "{r,m} -rf build",
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
    assert decision.allowed is False


def test_resolve_bash_command_permission_fails_closed_for_unparseable_commands() -> None:
    """Malformed shell input should fail closed rather than falling back to workspace-write."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "rm 'unterminated",
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
    assert decision.allowed is False


def test_resolve_bash_command_permission_escalates_for_dangerous_shell_commands() -> None:
    """Dangerous shell commands should require explicit dangerous-shell permission."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "rm -rf build",
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
    assert decision.allowed is False


def test_resolve_bash_command_permission_classifies_simple_grep_searches_as_read_only() -> None:
    """Simple grep searches over explicit paths should remain available to read-only runs."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "grep TODO README.md",
        tool,
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert decision.required_permission == ToolPermissionLevel.READ_ONLY
    assert decision.allowed is True


def test_resolve_bash_command_permission_classifies_safe_git_reads_as_read_only() -> None:
    """Common read-only git inspection commands should stay available in read-only mode."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    for command in (
        "git log --oneline",
        "git diff -- README.md",
        "git show HEAD~1",
        "git ls-files",
        "git rev-parse HEAD",
        "git blame README.md",
        "git grep TODO -- README.md",
    ):
        decision = resolve_bash_command_permission(
            command,
            tool,
            granted_permission=ToolPermissionLevel.READ_ONLY,
        )

        assert decision.required_permission == ToolPermissionLevel.READ_ONLY
        assert decision.allowed is True


def test_git_grep_without_pattern_stays_out_of_read_only() -> None:
    """Git grep should remain conservative when it lacks a real search pattern."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "git grep -",
        tool,
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert decision.required_permission == ToolPermissionLevel.WORKSPACE_WRITE
    assert decision.allowed is False


def test_resolve_bash_command_permission_does_not_treat_pattern_only_grep_as_read_only() -> None:
    """Grep without an explicit path can still block on stdin and should stay conservative."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "grep TODO",
        tool,
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert decision.required_permission == ToolPermissionLevel.WORKSPACE_WRITE
    assert decision.allowed is False


def test_resolve_bash_command_permission_escalates_for_push_and_deploy_commands() -> None:
    """Push and deploy actions should sit at the top of the permission ladder."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "git push origin HEAD",
        tool,
        granted_permission=ToolPermissionLevel.NETWORKED_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.GIT_PUSH_OR_DEPLOY
    assert decision.allowed is False
