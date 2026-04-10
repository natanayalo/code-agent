"""Typed git helper wrapper exposed through the internal tool layer."""

from __future__ import annotations

import json
import shlex
from enum import StrEnum

from pydantic import Field, ValidationError, model_validator

from tools.registry import ToolModel


class GitToolError(ValueError):
    """Raised when git helper input cannot be normalized into a safe command."""


class GitOperation(StrEnum):
    """Supported git helper operations for the first internal wrapper slice."""

    STATUS = "status"
    DIFF = "diff"
    BRANCH = "branch"
    COMMIT = "commit"


class GitToolRequest(ToolModel):
    """Normalized git helper request."""

    operation: GitOperation
    include_untracked: bool = True
    porcelain: bool = False
    staged: bool = False
    against: str | None = None
    pathspecs: tuple[str, ...] = Field(default_factory=tuple)
    show_current: bool = False
    branch_name: str | None = None
    create: bool = False
    message: str | None = None
    include_all: bool = False

    @model_validator(mode="after")
    def _validate_operation_specific_fields(self) -> GitToolRequest:
        if self.operation == GitOperation.STATUS:
            self._reject_fields(
                "against",
                "branch_name",
                "create",
                "include_all",
                "message",
                "show_current",
                "staged",
            )
            return self

        if self.operation == GitOperation.DIFF:
            self._reject_fields(
                "branch_name",
                "create",
                "include_all",
                "message",
                "porcelain",
                "show_current",
            )
            return self

        if self.operation == GitOperation.BRANCH:
            self._reject_fields(
                "against",
                "include_all",
                "include_untracked",
                "message",
                "pathspecs",
                "porcelain",
                "staged",
            )
            if self.create and not self.branch_name:
                raise GitToolError("Git branch creation requires branch_name.")
            if not self.create:
                self._reject_fields("branch_name")
            if self.show_current and self.create:
                raise GitToolError("Git branch requests cannot combine show_current and create.")
            return self

        if self.operation == GitOperation.COMMIT:
            self._reject_fields(
                "against",
                "branch_name",
                "create",
                "include_untracked",
                "pathspecs",
                "porcelain",
                "show_current",
                "staged",
            )
            if self.message is None or not self.message.strip():
                raise GitToolError("Git commit requests require a non-empty message.")
            return self

        raise GitToolError(f"Unsupported git operation: {self.operation}")

    def _reject_fields(self, *field_names: str) -> None:
        for field_name in field_names:
            if field_name not in self.model_fields_set:
                continue
            value = getattr(self, field_name)
            if value not in (None, False, (), ""):
                raise GitToolError(
                    f"Git {self.operation.value} requests do not support `{field_name}`."
                )


def parse_git_tool_input(raw_input: str) -> GitToolRequest:
    """Parse a JSON-encoded git helper request from runtime tool input."""
    try:
        payload = json.loads(raw_input)
    except json.JSONDecodeError as exc:
        raise GitToolError("Git helper input must be valid JSON.") from exc

    if not isinstance(payload, dict):
        raise GitToolError("Git helper input must decode to a JSON object.")

    try:
        return GitToolRequest.model_validate(payload)
    except ValidationError as exc:
        raise GitToolError(str(exc)) from exc


def build_git_command(request: GitToolRequest) -> str:
    """Render a safe shell command for one normalized git helper request."""
    tokens: list[str] = ["git"]

    if request.operation == GitOperation.STATUS:
        tokens.extend(["status"])
        if request.porcelain:
            tokens.append("--porcelain=v1")
        tokens.append(
            "--untracked-files=all" if request.include_untracked else "--untracked-files=no"
        )
        if request.pathspecs:
            tokens.append("--")
            tokens.extend(request.pathspecs)
        return shlex.join(tokens)

    if request.operation == GitOperation.DIFF:
        tokens.append("diff")
        if request.staged:
            tokens.append("--cached")
        if request.against is not None:
            tokens.append(request.against)
        if request.pathspecs:
            tokens.append("--")
            tokens.extend(request.pathspecs)
        return shlex.join(tokens)

    if request.operation == GitOperation.BRANCH:
        tokens.append("branch")
        if request.show_current:
            tokens.append("--show-current")
        elif request.create:
            assert request.branch_name is not None  # validated above
            tokens.append(request.branch_name)
        else:
            tokens.append("--list")
        return shlex.join(tokens)

    if request.operation == GitOperation.COMMIT:
        tokens.append("commit")
        if request.include_all:
            tokens.append("-a")
        assert request.message is not None  # validated above
        tokens.extend(["-m", request.message])
        return shlex.join(tokens)

    raise GitToolError(f"Unsupported git operation: {request.operation}")


def build_git_command_from_input(raw_input: str) -> str:
    """Parse runtime tool input and render the corresponding git shell command."""
    return build_git_command(parse_git_tool_input(raw_input))
