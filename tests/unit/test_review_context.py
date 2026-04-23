"""Unit tests for independent-review context packet packing."""

from workers.base import WorkerCommand
from workers.review_context import (
    DEFAULT_REVIEW_PACKET_MAX_FILES,
    pack_reviewer_context,
)


def test_pack_reviewer_context_limits_changed_files_list() -> None:
    files_changed = [f"file_{index:02d}.py" for index in range(DEFAULT_REVIEW_PACKET_MAX_FILES + 3)]

    packet = pack_reviewer_context(
        task_text="Update behavior",
        worker_summary="Implemented fixes",
        files_changed=files_changed,
        diff_text="+line\n",
    )

    omitted_count = len(files_changed) - DEFAULT_REVIEW_PACKET_MAX_FILES
    assert f"- ... {omitted_count} more files omitted" in packet
    for index in range(DEFAULT_REVIEW_PACKET_MAX_FILES):
        assert f"- file_{index:02d}.py" in packet
    assert f"- file_{DEFAULT_REVIEW_PACKET_MAX_FILES:02d}.py" not in packet


def test_pack_reviewer_context_truncation_keeps_closed_diff_fence() -> None:
    packet = pack_reviewer_context(
        task_text="T" * 1200,
        worker_summary="S" * 1200,
        files_changed=[f"file_{index}.py" for index in range(40)],
        diff_text="\n".join(f"+line_{index}" for index in range(600)),
        commands_run=[WorkerCommand(command="pytest -q", exit_code=0)],
        max_characters=900,
    )

    assert len(packet) <= 900
    assert "### Diff Excerpt" in packet
    assert "... (truncated)" in packet
    last_non_empty_line = [line for line in packet.splitlines() if line][-1]
    assert last_non_empty_line.startswith("```")
