"""Unit tests for the structured browser/search helper wrapper."""

from __future__ import annotations

import pytest

from tools import BrowserToolError, build_browser_command_from_input


def test_build_browser_command_from_input_supports_fetch() -> None:
    """Fetch requests should render deterministic curl commands."""
    command = build_browser_command_from_input(
        '{"operation":"fetch","url":"https://example.com/docs"}'
    )

    assert (
        command
        == "curl --silent --show-error --location --max-time=20 --url=https://example.com/docs"
    )


def test_build_browser_command_from_input_supports_search_with_default_limit() -> None:
    """Search requests should render deterministic curl commands with default limits."""
    command = build_browser_command_from_input('{"operation":"search","query":"langgraph"}')

    assert command == (
        "curl --silent --show-error --location --max-time=20 --get "
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
        "curl --silent --show-error --location --max-time=20 --get "
        "--url=https://en.wikipedia.org/w/api.php --data-urlencode=action=opensearch "
        "--data-urlencode=search=langgraph --data-urlencode=limit=3 "
        "--data-urlencode=namespace=0 --data-urlencode=format=json"
    )


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
