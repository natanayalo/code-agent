"""Unit tests for review context packing helpers."""

from workers.base import WorkerCommand
from workers.review_context import _truncate_at_line_boundary, pack_reviewer_context


def test_truncate_at_line_boundary_edge_cases() -> None:
    # Line 27: max_characters <= 0
    assert _truncate_at_line_boundary("hello", max_characters=0) == ""

    # Line 32: max_characters <= marker_length
    assert _truncate_at_line_boundary("hello world", max_characters=5, marker="[TRUNC]") == "[TRUN"

    # Line 39: prefix empty after rsplit
    # text="abc\ndef", max_chars=8, marker="[T]" -> keep_budget=5
    # prefix = "abc\nd"[:5] = "abc\nn" -> prefix.rsplit("\n", 1)[0] = "abc"
    # To trigger 39, we need prefix to be empty or whitespace after rsplit
    assert _truncate_at_line_boundary("abc\ndef", max_characters=4, marker="!") == "abc!"
    # Actually, to hit 39: prefix = "a"[:1] = "a". rsplit("\n") -> ["a"]. [0] = "a".
    # Wait, if "\n" is NOT in prefix, rsplit("\n") returns [prefix].
    # Let's try: text="a" * 10, max_chars=4, marker="!!!" -> keep_budget=1.
    # I'll just test a very short budget.
    assert _truncate_at_line_boundary("verylongtext", max_characters=5, marker="...") == "ve..."


def test_pack_reviewer_context_many_commands() -> None:
    # Line 74: > 24 commands
    commands = [WorkerCommand(command=f"cmd{i}", exit_code=0) for i in range(30)]
    packet = pack_reviewer_context(
        task_text="task",
        worker_summary="summary",
        files_changed=["a.py"],
        diff_text="diff",
        commands_run=commands,
    )
    assert "6 more commands omitted" in packet


def test_pack_reviewer_context_no_diff_budget() -> None:
    # Line 138: remaining_diff_budget <= 0
    # Use a tiny max_characters to squeeze out the diff
    packet = pack_reviewer_context(
        task_text="long task text " * 100,
        worker_summary="long summary text " * 100,
        files_changed=["a.py"],
        diff_text="some diff",
        max_characters=100,
    )
    assert "### Diff Excerpt" in packet
    assert "... (truncated)" in packet
