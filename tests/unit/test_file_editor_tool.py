"""Unit tests for structured file-view/search/edit tool wrappers."""

from __future__ import annotations

import pytest

import tools.file_editor as file_editor
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


def test_build_view_file_command_from_input_without_range_uses_full_file_template() -> None:
    """view_file should include the full-file numbered output program by default."""
    command = build_view_file_command_from_input('{"path":"src/main.py"}')

    assert command.startswith("awk ")
    assert 'printf("%6d  %s\\n", NR, $0)' in command


def test_build_view_file_command_from_input_with_start_line_only() -> None:
    """view_file should support open-ended ranges when only start_line is provided."""
    command = build_view_file_command_from_input('{"path":"src/main.py","start_line":7}')

    assert "NR>=7" in command
    assert "NR<=" not in command


def test_build_view_file_command_from_input_rejects_descending_range() -> None:
    """view_file should fail clearly when end_line is smaller than start_line."""
    with pytest.raises(FileEditorToolError, match="end_line"):
        build_view_file_command_from_input('{"path":"src/main.py","start_line":9,"end_line":2}')


def test_build_view_file_command_from_input_prefixes_equals_only_path_for_awk() -> None:
    """awk paths containing '=' should be prefixed so they cannot be parsed as assignments."""
    command = build_view_file_command_from_input('{"path":"config=v1.txt"}')

    assert "./config=v1.txt" in command


def test_build_view_file_command_from_input_prefixes_hyphen_path_for_awk() -> None:
    """awk paths starting with '-' should be prefixed to avoid option interpretation."""
    command = build_view_file_command_from_input('{"path":"-danger.txt"}')

    assert "./-danger.txt" in command


def test_build_search_file_command_from_input_supports_literal_mode() -> None:
    """search_file literal mode should map to fixed-string ripgrep flags."""
    command = build_search_file_command_from_input(
        '{"path":"src/main.py","query":"hello world","regex":false,"context_lines":1}'
    )

    assert command.startswith("rg ")
    assert "--fixed-strings" in command
    assert "--context=1" in command
    assert "src/main.py" in command


def test_build_search_file_command_from_input_defaults_to_regex_mode() -> None:
    """search_file should omit fixed-strings when regex mode is enabled."""
    command = build_search_file_command_from_input('{"path":"src/main.py","query":"hello.*"}')

    assert command.startswith("rg ")
    assert "--fixed-strings" not in command


def test_build_search_dir_command_from_input_requires_query() -> None:
    """search_dir requests should fail on missing query text."""
    with pytest.raises(FileEditorToolError, match="query"):
        build_search_dir_command_from_input('{"path":"src"}')


def test_build_search_dir_command_from_input_renders_recursive_search() -> None:
    """search_dir should render a recursive rg command for valid requests."""
    command = build_search_dir_command_from_input(
        '{"path":"src","query":"TODO","regex":false,"context_lines":3}'
    )

    assert command.startswith("rg ")
    assert "--fixed-strings" in command
    assert "--context=3" in command
    assert " src" in command


def test_build_str_replace_editor_command_from_input_renders_two_phase_update() -> None:
    """str_replace_editor should validate uniqueness before writing and moving temp output."""
    command = build_str_replace_editor_command_from_input(
        '{"path":"README.md","old_text":"hello","new_text":"hello world"}'
    )

    assert "CODEX_OLD_TEXT=hello" in command
    assert "CODEX_NEW_TEXT='hello world'" in command
    assert 'ENVIRON["CODEX_OLD_TEXT"]' in command
    assert 'ENVIRON["CODEX_NEW_TEXT"]' in command
    assert "idx+1" in command
    assert "str_replace_editor: old_text not found in file." in command
    assert "str_replace_editor: old_text is ambiguous" in command
    assert "> README.md.codex_tmp_replace" in command
    assert "mv -- README.md.codex_tmp_replace README.md" in command


def test_build_str_replace_editor_command_from_input_rejects_multiline_values() -> None:
    """str_replace_editor should reject multiline old/new text for deterministic shell edits."""
    with pytest.raises(FileEditorToolError, match="single-line text"):
        build_str_replace_editor_command_from_input(
            '{"path":"README.md","old_text":"hello\\nworld","new_text":"replacement"}'
        )


def test_build_str_replace_editor_command_from_input_rejects_multiline_new_text() -> None:
    """str_replace_editor should reject multiline replacement text."""
    with pytest.raises(FileEditorToolError, match="single-line text"):
        build_str_replace_editor_command_from_input(
            '{"path":"README.md","old_text":"hello","new_text":"replacement\\nvalue"}'
        )


def test_build_str_replace_editor_command_from_input_prefixes_equals_only_path_for_awk() -> None:
    """awk file paths containing '=' should be prefixed in both check and write stages."""
    command = build_str_replace_editor_command_from_input(
        '{"path":"config=v1.txt","old_text":"a","new_text":"b"}'
    )

    assert "./config=v1.txt" in command
    assert "./config=v1.txt.codex_tmp_replace" in command


def test_build_str_replace_editor_command_from_input_prefixes_hyphen_path_for_awk() -> None:
    """awk-side path usage should prefix hyphen-leading filenames in replacement mode."""
    command = build_str_replace_editor_command_from_input(
        '{"path":"-danger.txt","old_text":"a","new_text":"b"}'
    )

    assert "./-danger.txt" in command
    assert "./-danger.txt.codex_tmp_replace" in command


def test_file_tools_reject_invalid_json_payloads() -> None:
    """Malformed JSON should fail before model validation runs."""
    with pytest.raises(FileEditorToolError, match="valid JSON"):
        build_view_file_command_from_input("{bad json")


def test_file_tools_reject_non_object_json_payloads() -> None:
    """JSON inputs must decode into an object payload."""
    with pytest.raises(FileEditorToolError, match="JSON object"):
        build_search_file_command_from_input('"just a string"')


def test_file_tools_reject_whitespace_only_required_fields() -> None:
    """Whitespace-only required fields should be treated as empty by validators."""
    with pytest.raises(FileEditorToolError, match="non-empty `path`"):
        build_search_dir_command_from_input('{"path":"   ","query":"needle"}')


def test_build_view_file_command_from_input_wraps_validation_errors() -> None:
    """view_file parser should convert ValidationError into FileEditorToolError."""
    with pytest.raises(FileEditorToolError, match="validation failed"):
        build_view_file_command_from_input('{"path":"src/main.py","start_line":"x"}')


def test_build_search_file_command_from_input_wraps_validation_errors() -> None:
    """search_file parser should convert ValidationError into FileEditorToolError."""
    with pytest.raises(FileEditorToolError, match="validation failed"):
        build_search_file_command_from_input(
            '{"path":"src/main.py","query":"ok","context_lines":99}'
        )


def test_summarize_validation_error_handles_empty_error_list() -> None:
    """Fallback message should be used when no individual errors are available."""

    class _NoDetailsError:
        def errors(self, **kwargs):  # type: ignore[no-untyped-def]
            return []

    summary = file_editor._summarize_validation_error(_NoDetailsError())  # type: ignore[arg-type]

    assert summary == "File tool input validation failed."
