"""Unit tests for cli_runtime_files, self_review_packet, post_run_lint, prompt_review."""

from __future__ import annotations

from workers.cli_runtime_files import (
    _decode_safely,
    _git_status_unavailable,
    _parse_porcelain_lines,
    _parse_porcelain_z,
)
from workers.self_review_packet import (
    _extract_diff_line_hints,
    _normalize_diff_new_path,
    _truncate_block,
)


def test_decode_safely():
    assert _decode_safely(None) == ""
    assert _decode_safely("hello") == "hello"
    assert _decode_safely(b"world") == "world"
    assert _decode_safely(b"\xff\xfe") == "\ufffd\ufffd"


def test_git_status_unavailable():
    assert (
        _git_status_unavailable("fatal: not a git repository (or any of the parent directories)")
        is True
    )
    assert _git_status_unavailable("fatal: detected dubious ownership in repository") is True
    assert _git_status_unavailable("fatal: safe.directory error") is True
    assert _git_status_unavailable("M  file.py") is False


def test_parse_porcelain_z():
    # NUL delimited porcelain output format: "XY path\x00"
    raw = " M file1.py\x00 A file2.py\x00"
    paths = _parse_porcelain_z(raw)
    assert paths == ["file1.py", "file2.py"]


def test_parse_porcelain_lines():
    raw = " M file1.py\nA  file2.py\nR  old.py -> new.py\n"
    paths = _parse_porcelain_lines(raw)
    assert paths == ["file1.py", "file2.py", "new.py"]


def test_truncate_block():
    truncated = _truncate_block("hello world", 5)
    assert "truncated to 5 characters" in truncated
    assert _truncate_block("hi", 10) == "hi"


def test_normalize_diff_new_path():
    assert _normalize_diff_new_path("b/foo/bar.py") == "foo/bar.py"
    assert _normalize_diff_new_path("/dev/null") is None


def test_extract_diff_line_hints():
    diff = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -10,3 +10,4 @@
 line1
+added line
 line2
"""
    hints = _extract_diff_line_hints(diff)
    assert "test.py" in hints
