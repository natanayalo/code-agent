"""Typed browser/search helper wrapper exposed through the internal tool layer."""

from __future__ import annotations

import json
import shlex
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, model_validator

from tools.registry import DEFAULT_EXECUTE_BROWSER_TIMEOUT_SECONDS, ToolModel

_SEARCH_ENDPOINT = "https://en.wikipedia.org/w/api.php"


class BrowserToolError(ValueError):
    """Raised when browser helper input cannot be normalized into a safe command."""


class BrowserOperation(StrEnum):
    """Supported browser/search helper operations for the first wrapper slice."""

    FETCH = "fetch"
    SEARCH = "search"


class BrowserToolRequest(ToolModel):
    """Normalized browser/search helper request."""

    operation: BrowserOperation
    url: str | None = None
    query: str | None = None
    limit: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def _validate_request(self) -> BrowserToolRequest:
        if self.operation == BrowserOperation.FETCH:
            self._require_non_empty("url", self.url)
            assert self.url is not None
            _validate_http_url(self.url)
            self._reject_fields("query")
            limit_default = type(self).model_fields["limit"].default
            if "limit" in self.model_fields_set and self.limit != limit_default:
                raise BrowserToolError(
                    "Browser fetch requests do not support custom `limit` "
                    f"(default is {limit_default})."
                )
            return self

        if self.operation == BrowserOperation.SEARCH:
            self._require_non_empty("query", self.query)
            self._reject_fields("url")
            return self

        raise BrowserToolError(f"Unsupported browser operation: {self.operation}")

    def _reject_fields(self, *field_names: str) -> None:
        for field_name in field_names:
            if field_name not in self.model_fields_set:
                continue
            value = getattr(self, field_name)
            if value is not None:
                raise BrowserToolError(
                    f"Browser {self.operation.value} requests do not support `{field_name}`."
                )

    @staticmethod
    def _require_non_empty(field_name: str, value: str | None) -> None:
        if value is None or not value.strip():
            raise BrowserToolError(f"Browser requests require non-empty `{field_name}`.")


def _validate_http_url(url: str) -> None:
    """Validate that URLs use HTTP(S) with an explicit host."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BrowserToolError("Browser fetch requests require an http(s) URL with a host.")


def parse_browser_tool_input(raw_input: str) -> BrowserToolRequest:
    """Parse a JSON-encoded browser helper request from runtime tool input."""
    try:
        payload = json.loads(raw_input)
    except json.JSONDecodeError as exc:
        raise BrowserToolError("Browser helper input must be valid JSON.") from exc

    if not isinstance(payload, dict):
        raise BrowserToolError("Browser helper input must decode to a JSON object.")

    try:
        return BrowserToolRequest.model_validate(payload)
    except ValidationError as exc:
        raise BrowserToolError(_summarize_validation_error(exc)) from exc


def _summarize_validation_error(exc: ValidationError) -> str:
    """Render concise Pydantic validation errors for LLM-facing feedback."""
    errors = exc.errors(include_url=False, include_input=False)
    details: list[str] = []
    for error in errors:
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "__root__")
        message = str(error.get("msg", "invalid value"))
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        details.append(f"{location}: {message}" if location else message)

    if not details:
        return "Browser helper input validation failed."
    return f"Browser helper input validation failed: {'; '.join(details)}"


def build_browser_command(
    request: BrowserToolRequest,
    *,
    timeout_seconds: int | None = None,
) -> str:
    """Render a safe shell command for one normalized browser/search request."""
    resolved_timeout_seconds = (
        DEFAULT_EXECUTE_BROWSER_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    tokens: list[str] = [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        f"--max-time={resolved_timeout_seconds}",
        "--globoff",
    ]

    if request.operation == BrowserOperation.FETCH:
        assert request.url is not None
        tokens.append(f"--url={request.url}")
        return shlex.join(tokens)

    if request.operation == BrowserOperation.SEARCH:
        assert request.query is not None
        tokens.extend(
            [
                "--get",
                f"--url={_SEARCH_ENDPOINT}",
                "--data-urlencode=action=opensearch",
                f"--data-urlencode=search={request.query}",
                f"--data-urlencode=limit={request.limit}",
                "--data-urlencode=namespace=0",
                "--data-urlencode=format=json",
            ]
        )
        return shlex.join(tokens)

    raise BrowserToolError(f"Unsupported browser operation: {request.operation}")


def build_browser_command_from_input(
    raw_input: str,
    *,
    timeout_seconds: int | None = None,
) -> str:
    """Parse runtime tool input and render the corresponding browser helper command."""
    return build_browser_command(
        parse_browser_tool_input(raw_input),
        timeout_seconds=timeout_seconds,
    )
