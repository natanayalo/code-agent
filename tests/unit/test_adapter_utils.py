"""Unit tests for shared adapter utility helpers."""

from __future__ import annotations

from workers.adapter_utils import (
    build_failure_summary,
    coerce_bool,
    coerce_positive_int,
    normalize_prompt_override,
    truncate_detail_keep_head,
    truncate_detail_keep_tail,
)


def test_build_failure_summary_concatenates_inputs() -> None:
    """The failure summary should combine final message and raw summary."""
    assert build_failure_summary(summary="base", final_message="Final.") == "Final. base"
    assert build_failure_summary(summary="base", final_message=None) == "base"
    assert build_failure_summary(summary=None, final_message="Final.") == "Final."
    assert build_failure_summary(summary="   ", final_message="Final.") == "Final."
    assert build_failure_summary(summary="base", final_message="   ") == "base"
    assert build_failure_summary(summary=None, final_message=None) == ""


def test_coerce_positive_int_parses_supported_inputs() -> None:
    """Positive ints should parse from numbers/strings and default otherwise."""
    assert coerce_positive_int(42, default=5) == 42
    assert coerce_positive_int(12.9, default=5) == 12
    assert coerce_positive_int("7", default=5) == 7
    assert coerce_positive_int(" 10.2 ", default=5) == 10

    assert coerce_positive_int(True, default=5) == 5  # noqa: FBT003
    assert coerce_positive_int(0, default=5) == 5
    assert coerce_positive_int(-1, default=5) == 5
    assert coerce_positive_int("0", default=5) == 5
    assert coerce_positive_int("abc", default=5) == 5
    assert coerce_positive_int("", default=5) == 5
    assert coerce_positive_int([], default=5) == 5


def test_truncate_detail_keep_tail_behaviour() -> None:
    """Tail truncation should preserve suffix with the legacy marker prefix."""
    assert truncate_detail_keep_tail("   ", max_characters=5) == "<empty>"
    assert truncate_detail_keep_tail("hello", max_characters=10) == "hello"
    assert truncate_detail_keep_tail("abcdefghijkl", max_characters=4) == "[truncated]...ijkl"


def test_normalize_prompt_override_behaviour() -> None:
    """Prompt override normalization should trim text and collapse blank inputs."""
    assert normalize_prompt_override(None) is None
    assert normalize_prompt_override("   ") is None
    assert normalize_prompt_override("  run review  ") == "run review"


def test_coerce_bool_parses_supported_inputs() -> None:
    """Boolean coercion should parse env-style values and default unknowns."""
    assert coerce_bool(True, default=False) is True
    assert coerce_bool("true", default=False) is True
    assert coerce_bool(" YES ", default=False) is True
    assert coerce_bool("0", default=True) is False
    assert coerce_bool("off", default=True) is False
    assert coerce_bool("unknown", default=True) is True


def test_truncate_detail_keep_head_behaviour() -> None:
    """Head truncation should preserve prefix with the legacy marker suffix."""
    assert truncate_detail_keep_head("   ", max_characters=5) == "<empty>"
    assert truncate_detail_keep_head("hello", max_characters=10) == "hello"
    assert truncate_detail_keep_head("abcdefghijkl", max_characters=4) == "abcd...[truncated]"
