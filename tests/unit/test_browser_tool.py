"""Unit tests for the structured browser/search helper wrapper."""

from __future__ import annotations

import shlex

import pytest

from tools import (
    DEFAULT_EXECUTE_BROWSER_TIMEOUT_SECONDS,
    BrowserToolError,
    build_browser_command_from_input,
)

_CURL_PREFIX = (
    "curl --fail --silent --show-error --location "
    f"--max-time={DEFAULT_EXECUTE_BROWSER_TIMEOUT_SECONDS} --globoff"
)


def test_build_browser_command_from_input_supports_fetch() -> None:
    """Fetch requests should render deterministic curl commands."""
    command = build_browser_command_from_input(
        '{"operation":"fetch","url":"https://example.com/docs"}'
    )

    assert command == f"{_CURL_PREFIX} --url=https://example.com/docs"


def test_build_browser_command_from_input_supports_custom_timeout_override() -> None:
    """The optional timeout override should flow into the rendered curl command."""
    command = build_browser_command_from_input(
        '{"operation":"fetch","url":"https://example.com/docs"}',
        timeout_seconds=9,
    )

    assert command == (
        "curl --fail --silent --show-error --location "
        "--max-time=9 --globoff --url=https://example.com/docs"
    )


def test_build_browser_command_from_input_supports_search_with_default_limit() -> None:
    """Search requests should render deterministic curl commands with default limits."""
    command = build_browser_command_from_input('{"operation":"search","query":"langgraph"}')

    assert command == (
        f"{_CURL_PREFIX} --get "
        "--url=https://en.wikipedia.org/w/api.php --data-urlencode=action=opensearch "
        "--data-urlencode=search=langgraph --data-urlencode=limit=5 "
        "--data-urlencode=namespace=0 --data-urlencode=format=json"
    )


def test_build_browser_command_from_input_supports_search_with_explicit_limit() -> None:
    """Search requests should preserve explicit result limits."""
    command = build_browser_command_from_input(
        '{"operation":"search","query":"langgraph","limit":3}'
    )

    assert command == (
        f"{_CURL_PREFIX} --get "
        "--url=https://en.wikipedia.org/w/api.php --data-urlencode=action=opensearch "
        "--data-urlencode=search=langgraph --data-urlencode=limit=3 "
        "--data-urlencode=namespace=0 --data-urlencode=format=json"
    )


def test_build_browser_command_from_input_allows_fetch_with_explicit_default_limit() -> None:
    """Fetch requests may include the default limit value without failing validation."""
    command = build_browser_command_from_input(
        '{"operation":"fetch","url":"https://example.com/docs","limit":5}'
    )

    assert command == f"{_CURL_PREFIX} --url=https://example.com/docs"


def test_build_browser_command_from_input_rejects_fetch_with_custom_limit() -> None:
    """Fetch requests should reject non-default limits."""
    with pytest.raises(BrowserToolError, match="custom `limit`"):
        build_browser_command_from_input(
            '{"operation":"fetch","url":"https://example.com/docs","limit":3}'
        )


@pytest.mark.parametrize(
    ("query_input", "expected_query"),
    [
        ("@private.txt", "@private.txt"),
        ("<private.txt", "<private.txt"),
        ("=private.txt", "=private.txt"),
    ],
)
def test_build_browser_command_from_input_preserves_literal_query_prefixes(
    query_input: str,
    expected_query: str,
) -> None:
    """Search queries should remain literal when passed with the name=content curl form."""
    command = build_browser_command_from_input(
        f'{{"operation":"search","query":"{query_input}","limit":3}}'
    )

    tokens = shlex.split(command)

    assert "--globoff" in tokens
    assert f"--max-time={DEFAULT_EXECUTE_BROWSER_TIMEOUT_SECONDS}" in tokens
    assert "--data-urlencode=action=opensearch" in tokens
    assert f"--data-urlencode=search={expected_query}" in tokens


def test_build_browser_command_from_input_rejects_invalid_json() -> None:
    """The browser helper should fail clearly when runtime input is invalid JSON."""
    with pytest.raises(BrowserToolError, match="valid JSON"):
        build_browser_command_from_input("search")


def test_build_browser_command_from_input_rejects_non_object_json() -> None:
    """The browser helper should require an object payload."""
    with pytest.raises(BrowserToolError, match="JSON object"):
        build_browser_command_from_input('["search"]')


def test_build_browser_command_from_input_rejects_fetch_without_url() -> None:
    """Fetch operations should require a URL."""
    with pytest.raises(BrowserToolError, match="non-empty `url`"):
        build_browser_command_from_input('{"operation":"fetch"}')


def test_build_browser_command_from_input_rejects_non_http_fetch_url() -> None:
    """Fetch operations should require http(s) URLs."""
    with pytest.raises(BrowserToolError, match="http"):
        build_browser_command_from_input('{"operation":"fetch","url":"file:///etc/passwd"}')


def test_build_browser_command_from_input_rejects_search_only_fields_on_fetch() -> None:
    """Fetch operations should reject search-only fields."""
    with pytest.raises(BrowserToolError, match="do not support `query`"):
        build_browser_command_from_input(
            '{"operation":"fetch","url":"https://example.com/docs","query":"langgraph"}'
        )


def test_build_browser_command_from_input_rejects_fetch_only_fields_on_search() -> None:
    """Search operations should reject fetch-only fields."""
    with pytest.raises(BrowserToolError, match="do not support `url`"):
        build_browser_command_from_input(
            '{"operation":"search","query":"langgraph","url":"https://example.com/docs"}'
        )
