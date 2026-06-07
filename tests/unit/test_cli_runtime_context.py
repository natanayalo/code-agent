# ruff: noqa: F403, F405
"""Behavior-focused CLI runtime tests."""

from __future__ import annotations

from tests.unit.cli_runtime_support import *  # noqa: F403


def test_extract_file_hints_includes_extensionless_root_file_arguments() -> None:
    """Heuristics should include common extensionless root-path file arguments."""
    assert "main.py" in _extract_file_hints_from_command("awk '{print $1}' main.py")
    assert "VERSION" in _extract_file_hints_from_command("cat VERSION")
    assert "LICENSE" in _extract_file_hints_from_command("rm LICENSE")
    assert "install" not in _extract_file_hints_from_command("pip install pytest")
    assert "LICENSE" in _extract_file_hints_from_command("grep TODO LICENSE")
    assert "TODO" not in _extract_file_hints_from_command("grep TODO LICENSE")
    assert "manage.py" in _extract_file_hints_from_command("python manage.py")
    assert "build" in _extract_file_hints_from_command("mkdir build")
    assert "build" in _extract_file_hints_from_command("rmdir build")
    assert "LICENSE" in _extract_file_hints_from_command("chmod 644 LICENSE")
    assert "root" not in _extract_file_hints_from_command("chown root LICENSE")
    assert "LICENSE" in _extract_file_hints_from_command("chown root LICENSE")
    assert "s/foo/bar/" not in _extract_file_hints_from_command("sed s/foo/bar/ LICENSE")
    assert "LICENSE" in _extract_file_hints_from_command("sed s/foo/bar/ LICENSE")
    assert "LICENSE" in _extract_file_hints_from_command("git add LICENSE")
    assert "status" not in _extract_file_hints_from_command("git status")
    assert "main" not in _extract_file_hints_from_command("git checkout main")


def test_extract_file_hints_handles_compound_shell_commands() -> None:
    """Heuristics should reset command context across shell separators."""
    hints = _extract_file_hints_from_command(
        "cat file1 && ls file2 | grep needle file3 |& cat file4 & cat file5"
    )

    assert "file1" in hints
    assert "file2" in hints
    assert "file3" in hints
    assert "file4" in hints
    assert "file5" in hints
    assert "ls" not in hints
    assert "grep" not in hints
    assert "needle" not in hints


def test_extract_file_hints_skips_redirection_tokens() -> None:
    """Shell redirection operators should not be classified as file hints."""
    hints = _extract_file_hints_from_command(
        "cat input.txt < src.txt > out.txt 2>&1 1> one.txt 1>> one-append.txt "
        "&> both.txt &>> both-append.txt >| force.txt |& grep failure"
    )

    assert "input.txt" in hints
    assert "src.txt" in hints
    assert "out.txt" in hints
    assert "one.txt" in hints
    assert "one-append.txt" in hints
    assert "both.txt" in hints
    assert "both-append.txt" in hints
    assert "force.txt" in hints
    assert "<" not in hints
    assert "2>&1" not in hints
    assert "1>" not in hints
    assert "1>>" not in hints
    assert "&>" not in hints
    assert "&>>" not in hints
    assert ">|" not in hints
    assert "|&" not in hints


def test_extract_file_hints_skips_current_and_parent_directory_tokens() -> None:
    """Directory shorthand tokens should not be treated as touched file hints."""
    hints = _extract_file_hints_from_command("ls . && cat ../notes.txt && cat ..")

    assert "../notes.txt" in hints
    assert "." not in hints
    assert ".." not in hints


def test_looks_read_only_command_uses_word_boundary_for_short_commands() -> None:
    """Short read-only commands should not match unrelated command-name prefixes."""
    assert _looks_read_only_command("ls") is True
    assert _looks_read_only_command("pwd") is True
    assert _looks_read_only_command("ls src") is True
    assert _looks_read_only_command("pwd /tmp") is True
    assert _looks_read_only_command("lsrc") is False
    assert _looks_read_only_command("pwd_helper") is False
    assert _looks_read_only_command("grep TODO README.md") is True
    assert _looks_read_only_command("awk '{print $1}' README.md") is True
    assert _looks_read_only_command("grep TODO README.md>out.txt") is False
    assert _looks_read_only_command("ls | tee output.txt") is False
    assert _looks_read_only_command("patch < fix.patch") is False
    assert _looks_read_only_command("git apply fix.patch") is False
    assert _looks_read_only_command("sed -i 's/a/b/' file.txt") is False


def test_build_condensed_context_summary_truncation_stays_within_budget() -> None:
    """Truncation notice should fit inside the configured summary character budget."""
    older_messages = [
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "printf 'hello world' > src/long_name.py\n```"
            ),
        ),
        CliRuntimeMessage(
            role="tool",
            tool_name="execute_bash",
            content=(
                "Tool result: execute_bash\nCommand: printf 'hello world' > src/long_name.py\n"
                "Exit code: 0\nDuration seconds: 0.250\nOutput:\n```text\n" + ("x" * 300) + "\n```"
            ),
        ),
    ]
    max_characters = 120
    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=max_characters,
    )

    assert len(summary) <= max_characters
    assert summary.endswith("characters]")


def test_build_condensed_context_summary_prefers_most_recent_file_hints() -> None:
    """File hints should keep chronological order and show the most recent entries."""
    older_messages = [
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "touch f1 f2 f3 f4 f5 f6 f7 f8 f9 f10\n```"
            ),
        ),
        CliRuntimeMessage(
            role="tool",
            tool_name="execute_bash",
            content=(
                "Tool result: execute_bash\nCommand: touch f1 f2 f3 f4 f5 f6 f7 f8 f9 f10\n"
                "Exit code: 0\nDuration seconds: 0.250\nOutput:\n```text\nok\n```"
            ),
        ),
    ]
    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=5000,
    )

    assert "- Files touched hints: `f3`, `f4`, `f5`, `f6`, `f7`, `f8`, `f9`, `f10`" in summary
    assert "`f1`" not in summary


def test_build_condensed_context_summary_escapes_backticks_in_inline_code() -> None:
    """Commands containing backticks should remain valid inline markdown/code text."""
    older_messages = [
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "echo `date` > out.txt\n```"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=2000,
    )

    assert "``echo `date` > out.txt``" in summary


def test_build_condensed_context_summary_escapes_edge_backticks_in_inline_code() -> None:
    """Inline-code rendering should stay valid when text starts/ends with backticks."""
    older_messages = [
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "`date`\n```"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=2000,
    )

    assert "`` `date` ``" in summary


def test_build_condensed_context_summary_escapes_backticks_in_current_state() -> None:
    """Current-state command formatting should remain valid when commands contain backticks."""
    older_messages = [
        CliRuntimeMessage(
            role="tool",
            tool_name="execute_bash",
            content=(
                "Tool result: execute_bash\nCommand: echo `date` > out.txt\n"
                "Exit code: 0\nDuration seconds: 0.250\nOutput:\n```text\nok\n```"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=2000,
    )

    assert "last command ``echo `date` > out.txt`` exited with code 0" in summary


def test_build_condensed_context_summary_parses_bash_fence_with_trailing_tag_spaces() -> None:
    """Command extraction should tolerate optional spaces after the bash language tag."""
    older_messages = [
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash   \n"
                "touch spaced_tag.txt\n```"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=2000,
    )

    assert "`touch spaced_tag.txt`" in summary


def test_build_condensed_context_summary_parses_text_fence_with_trailing_tag_spaces() -> None:
    """Output excerpt extraction should tolerate optional spaces after the text tag."""
    older_messages = [
        CliRuntimeMessage(
            role="tool",
            tool_name="execute_bash",
            content=(
                "Tool result: execute_bash\nCommand: pytest -q\n"
                "Exit code: 1\nDuration seconds: 0.250\nOutput:\n```text \n"
                "F tests/test_flow.py::test_case\n```\n"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=2000,
    )

    assert "exit 1 (F tests/test_flow.py::test_case)" in summary


def test_build_condensed_context_summary_uses_last_non_empty_output_line() -> None:
    """Error excerpt should prefer the final non-empty output line."""
    older_messages = [
        CliRuntimeMessage(
            role="tool",
            tool_name="execute_bash",
            content=(
                "Tool result: execute_bash\nCommand: pytest -q\n"
                "Exit code: 1\nDuration seconds: 0.250\nOutput:\n```text\n"
                "header line\n"
                "\n"
                "final failure detail\n"
                "```\n"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=2000,
    )

    assert "exit 1 (final failure detail)" in summary


def test_messages_for_adapter_turn_preserves_history_when_trimming_recent_tail() -> None:
    """Messages dropped from recent tail should be merged into summary, not lost."""
    messages = [
        CliRuntimeMessage(role="system", content="System prompt"),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "touch very_old.txt\n```\n" + ("w" * 900)
            ),
        ),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "touch older.txt\n```\n" + ("x" * 900)
            ),
        ),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "touch moved_to_summary.txt\n```\n" + ("y" * 900)
            ),
        ),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "touch stays_recent.txt\n```\n" + ("z" * 900)
            ),
        ),
    ]

    condensed = _messages_for_adapter_turn(
        messages,
        settings=CliRuntimeSettings(
            context_condenser_threshold_characters=1200,
            context_condenser_recent_messages=3,
            context_condenser_summary_max_characters=800,
            max_iterations=2,
            worker_timeout_seconds=30,
        ),
    )

    assert condensed[1].role == "assistant"
    assert "moved_to_summary.txt" in condensed[1].content
    assert all("moved_to_summary.txt" not in message.content for message in condensed[2:])
    assert any("touch stays_recent.txt" in message.content for message in condensed[2:])


def test_messages_for_adapter_turn_rebuilds_compact_summary_with_truncation_notice() -> None:
    """Final compact summary should be rebuilt and retain structured truncation metadata."""
    long_name_1 = "very_old_" + ("a" * 220) + ".txt"
    long_name_2 = "older_" + ("b" * 220) + ".txt"
    long_name_3 = "keep_recent_1_" + ("c" * 220) + ".txt"
    long_name_4 = "keep_recent_2_" + ("d" * 220) + ".txt"
    messages = [
        CliRuntimeMessage(role="system", content="System prompt\n" + ("s" * 500)),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                f"touch {long_name_1}\n```\n" + ("w" * 900)
            ),
        ),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                f"touch {long_name_2}\n```\n" + ("x" * 900)
            ),
        ),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                f"touch {long_name_3}\n```\n" + ("y" * 600)
            ),
        ),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                f"touch {long_name_4}\n```\n" + ("z" * 600)
            ),
        ),
    ]

    condensed = _messages_for_adapter_turn(
        messages,
        settings=CliRuntimeSettings(
            context_condenser_threshold_characters=1024,
            context_condenser_recent_messages=2,
            context_condenser_summary_max_characters=800,
            max_iterations=2,
            worker_timeout_seconds=30,
        ),
    )

    assert condensed[1].role == "assistant"
    assert "Condensed context summary" in condensed[1].content
    assert condensed[1].content.endswith("characters]")


def test_build_condensed_context_summary_prefers_latest_unique_file_occurrences() -> None:
    """Deduping should preserve latest unique file mentions before applying the tail window."""
    older_messages = [
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "touch f1 f2 f3 f4 f5 f6 f7 f8 f9 f1\n```"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=5000,
    )

    assert "- Files touched hints: `f3`, `f4`, `f5`, `f6`, `f7`, `f8`, `f9`, `f1`" in summary
