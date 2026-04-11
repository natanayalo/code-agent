"""Typed GitHub helper wrapper exposed through the internal tool layer."""

from __future__ import annotations

import json
import re
import shlex
from enum import StrEnum

from pydantic import Field, ValidationError, model_validator

from tools.registry import ToolModel

_REPOSITORY_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_REPOSITORY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-][A-Za-z0-9_.-]{0,99}$")
_RESERVED_REPOSITORY_NAMES = frozenset({".", ".."})


class GitHubToolError(ValueError):
    """Raised when GitHub helper input cannot be normalized into a safe command."""


class GitHubOperation(StrEnum):
    """Supported GitHub helper operations for the first internal wrapper slice."""

    PR_CREATE_DRAFT = "pr_create_draft"
    PR_COMMENT = "pr_comment"


class GitHubToolRequest(ToolModel):
    """Normalized GitHub helper request."""

    operation: GitHubOperation
    repository_full_name: str
    base_branch: str | None = None
    head_branch: str | None = None
    title: str | None = None
    body: str | None = None
    pr_number: int | None = Field(default=None, ge=1)
    comment_body: str | None = None

    @model_validator(mode="after")
    def _validate_request(self) -> GitHubToolRequest:
        owner, separator, repository_name = self.repository_full_name.partition("/")
        if (
            not separator
            or not _REPOSITORY_OWNER_PATTERN.fullmatch(owner)
            or not _REPOSITORY_NAME_PATTERN.fullmatch(repository_name)
            or repository_name in _RESERVED_REPOSITORY_NAMES
            or repository_name.lower().endswith(".git")
        ):
            raise GitHubToolError(
                "GitHub requests require repository_full_name in 'owner/name' format."
            )

        if self.operation == GitHubOperation.PR_CREATE_DRAFT:
            self._require_non_empty("base_branch", self.base_branch)
            self._require_non_empty("head_branch", self.head_branch)
            self._require_non_empty("title", self.title)
            self._require_non_empty("body", self.body)
            self._reject_fields("pr_number", "comment_body")
            self._reject_hyphen_prefix("base_branch", self.base_branch)
            self._reject_hyphen_prefix("head_branch", self.head_branch)
            return self

        if self.operation == GitHubOperation.PR_COMMENT:
            if self.pr_number is None:
                raise GitHubToolError("GitHub pr_comment requests require `pr_number`.")
            self._require_non_empty("comment_body", self.comment_body)
            self._reject_fields("base_branch", "head_branch", "title", "body")
            return self

        raise GitHubToolError(f"Unsupported GitHub operation: {self.operation}")

    def _reject_fields(self, *field_names: str) -> None:
        for field_name in field_names:
            if field_name not in self.model_fields_set:
                continue
            value = getattr(self, field_name)
            if value is not None:
                raise GitHubToolError(
                    f"GitHub {self.operation.value} requests do not support `{field_name}`."
                )

    @staticmethod
    def _require_non_empty(field_name: str, value: str | None) -> None:
        if value is None or not value.strip():
            raise GitHubToolError(f"GitHub requests require non-empty `{field_name}`.")

    @staticmethod
    def _reject_hyphen_prefix(field_name: str, value: str | None) -> None:
        if value is not None and value.startswith("-"):
            raise GitHubToolError(f"GitHub `{field_name}` cannot start with a hyphen.")


def parse_github_tool_input(raw_input: str) -> GitHubToolRequest:
    """Parse a JSON-encoded GitHub helper request from runtime tool input."""
    try:
        payload = json.loads(raw_input)
    except json.JSONDecodeError as exc:
        raise GitHubToolError("GitHub helper input must be valid JSON.") from exc

    if not isinstance(payload, dict):
        raise GitHubToolError("GitHub helper input must decode to a JSON object.")

    try:
        return GitHubToolRequest.model_validate(payload)
    except ValidationError as exc:
        raise GitHubToolError(_summarize_validation_error(exc)) from exc


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
        return "GitHub helper input validation failed."
    return f"GitHub helper input validation failed: {'; '.join(details)}"


def build_github_command(request: GitHubToolRequest) -> str:
    """Render a safe shell command for one normalized GitHub helper request."""
    tokens: list[str] = ["gh", "pr"]

    if request.operation == GitHubOperation.PR_CREATE_DRAFT:
        assert request.base_branch is not None
        assert request.head_branch is not None
        assert request.title is not None
        assert request.body is not None
        tokens.extend(
            [
                "create",
                "--draft",
                f"--repo={request.repository_full_name}",
                f"--base={request.base_branch}",
                f"--head={request.head_branch}",
                f"--title={request.title}",
                f"--body={request.body}",
            ]
        )
        return shlex.join(tokens)

    if request.operation == GitHubOperation.PR_COMMENT:
        assert request.pr_number is not None
        assert request.comment_body is not None
        tokens.extend(
            [
                "comment",
                str(request.pr_number),
                f"--repo={request.repository_full_name}",
                f"--body={request.comment_body}",
            ]
        )
        return shlex.join(tokens)

    raise GitHubToolError(f"Unsupported GitHub operation: {request.operation}")


def build_github_command_from_input(raw_input: str) -> str:
    """Parse runtime tool input and render the corresponding GitHub helper command."""
    return build_github_command(parse_github_tool_input(raw_input))
