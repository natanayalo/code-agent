"""Unit tests for shared private-tag redaction helpers."""

from __future__ import annotations

from privacy.redaction import (
    PRIVATE_TAG_REPLACEMENT,
    redact_private_tags,
    redact_private_tags_recursive,
)


def test_redact_private_tags_basic():
    assert redact_private_tags("") == ("", False)
    assert redact_private_tags("hello world") == ("hello world", False)

    text = "Authorization: Bearer <private>secret-token</private>"
    redacted, was_redacted = redact_private_tags(text)
    assert was_redacted is True
    assert redacted == f"Authorization: Bearer {PRIVATE_TAG_REPLACEMENT}"


def test_redact_private_tags_case_and_multiline():
    text = "Key: <PRIVATE>\nline1\nline2\n</PRIVATE> done"
    redacted, was_redacted = redact_private_tags(text)
    assert was_redacted is True
    assert redacted == f"Key: {PRIVATE_TAG_REPLACEMENT} done"


def test_redact_private_tags_multiple_and_unclosed():
    text = "<private>a</private> middle <Private>b</Private> end"
    redacted, was_redacted = redact_private_tags(text)
    assert was_redacted is True
    assert redacted == f"{PRIVATE_TAG_REPLACEMENT} middle {PRIVATE_TAG_REPLACEMENT} end"

    unclosed = "prefix <private>no closing tag here"
    redacted_unclosed, was_redacted = redact_private_tags(unclosed)
    assert was_redacted is False
    assert redacted_unclosed == unclosed


def test_redact_private_tags_recursive_containers():
    data = {
        "<private>key_secret</private>": "normal_val",
        "nested_dict": {"field": "<private>val_secret</private>"},
        "list_field": ["ok", "<private>item_secret</private>", 42],
        "tuple_field": ("a", "<private>t_secret</private>"),
        "set_field": {"<private>s_secret</private>"},
        "frozenset_field": frozenset({"<private>fs_secret</private>"}),
        "int_field": 123,
    }
    redacted_data, was_redacted = redact_private_tags_recursive(data)
    assert was_redacted is True
    assert PRIVATE_TAG_REPLACEMENT in redacted_data
    assert redacted_data["nested_dict"]["field"] == PRIVATE_TAG_REPLACEMENT
    assert redacted_data["list_field"][1] == PRIVATE_TAG_REPLACEMENT
    assert redacted_data["tuple_field"][1] == PRIVATE_TAG_REPLACEMENT
    assert PRIVATE_TAG_REPLACEMENT in redacted_data["set_field"]
    assert PRIVATE_TAG_REPLACEMENT in redacted_data["frozenset_field"]
    assert redacted_data["int_field"] == 123

    # Non-container primitive
    res, redacted = redact_private_tags_recursive(12345)
    assert res == 12345
    assert redacted is False
