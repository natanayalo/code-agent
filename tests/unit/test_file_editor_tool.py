"""Unit tests for structured file-view/search/edit tool wrappers."""

from __future__ import annotations

import pytest

from tools import (
    FileEditorToolError,
    build_search_dir_command_from_input,
    build_search_file_command_from_input,
    build_str_replace_editor_command_from_input,
    build_view_file_command_from_input,
)


def test_build_view_file_command_from_input_renders_line_range() -> None:
    """view_file should render an awk command that preserves line-number boundaries."""
    command = build_view_file_command_from_input(
        '{"path":"src/main.py","start_line":4,"end_line":8}'
    )

    assert command.startswith("awk ")
    assert "NR>=4 && NR<=8" in command
    assert "src/main.py" in command


def test_build_search_file_command_from_input_supports_literal_mode() -> None:
    """search_file literal mode should map to fixed-string ripgrep flags."""
    command = build_search_file_command_from_input(
        '{"path":"src/main.py","query":"hello world","regex":false,"context_lines":1}'
    )

    assert command.startswith("rg ")
    assert "--fixed-strings" in command
    assert "--context=1" in command
    assert "src/main.py" in command


def test_build_search_dir_command_from_input_requires_query() -> None:
    """search_dir requests should fail on missing query text."""
    with pytest.raises(FileEditorToolError, match="query"):
        build_search_dir_command_from_input('{"path":"src"}')


def test_build_str_replace_editor_command_from_input_renders_two_phase_update() -> None:
    """str_replace_editor should validate uniqueness before writing and moving temp output."""
    command = build_str_replace_editor_command_from_input(
        '{"path":"README.md","old_text":"hello","new_text":"hello world"}'
    )

    assert "str_replace_editor: old_text not found in file." in command
    assert "str_replace_editor: old_text is ambiguous" in command
    assert "> README.md.codex_tmp_replace" in command
    assert "mv README.md.codex_tmp_replace README.md" in command


def test_build_str_replace_editor_command_from_input_rejects_multiline_values() -> None:
    """str_replace_editor should reject multiline old/new text for deterministic shell edits."""
    with pytest.raises(FileEditorToolError, match="single-line text"):
        build_str_replace_editor_command_from_input(
            '{"path":"README.md","old_text":"hello\\nworld","new_text":"replacement"}'
        )
