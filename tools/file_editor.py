"""Structured file-view/search/edit tool wrappers exposed through the internal tool layer."""

from __future__ import annotations

import hashlib
import json
import shlex

from pydantic import Field, ValidationError, model_validator

from tools.registry import ToolModel


class FileEditorToolError(ValueError):
    """Raised when structured file tool input cannot be normalized safely."""


class ViewFileToolRequest(ToolModel):
    """Normalized view-file request."""

    path: str = Field(min_length=1)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> ViewFileToolRequest:
        _require_non_empty("path", self.path)
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise FileEditorToolError("view_file `end_line` cannot be smaller than `start_line`.")
        return self


class SearchFileToolRequest(ToolModel):
    """Normalized search-file request."""

    path: str = Field(min_length=1)
    query: str = Field(min_length=1)
    regex: bool = True
    context_lines: int = Field(default=2, ge=0, le=20)

    @model_validator(mode="after")
    def _validate_request(self) -> SearchFileToolRequest:
        _require_non_empty("path", self.path)
        _require_non_empty("query", self.query, allow_whitespace=True)
        return self


class SearchDirToolRequest(ToolModel):
    """Normalized recursive directory-search request."""

    path: str = Field(default=".", min_length=1)
    query: str = Field(min_length=1)
    regex: bool = True
    context_lines: int = Field(default=2, ge=0, le=20)

    @model_validator(mode="after")
    def _validate_request(self) -> SearchDirToolRequest:
        _require_non_empty("path", self.path)
        _require_non_empty("query", self.query, allow_whitespace=True)
        return self


class StrReplaceEditorToolRequest(ToolModel):
    """Normalized exact-string replacement request."""

    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str

    @model_validator(mode="after")
    def _validate_request(self) -> StrReplaceEditorToolRequest:
        _require_non_empty("path", self.path)
        _require_non_empty("old_text", self.old_text, allow_whitespace=True)
        if "\n" in self.old_text or "\r" in self.old_text:
            raise FileEditorToolError("str_replace_editor `old_text` must be single-line text.")
        if "\n" in self.new_text or "\r" in self.new_text:
            raise FileEditorToolError("str_replace_editor `new_text` must be single-line text.")
        return self


def _require_non_empty(
    field_name: str,
    value: str | None,
    *,
    allow_whitespace: bool = False,
) -> None:
    if value is None or (not value if allow_whitespace else not value.strip()):
        raise FileEditorToolError(f"File tool requests require non-empty `{field_name}`.")


def _parse_json_object(raw_input: str) -> dict[str, object]:
    try:
        payload = json.loads(raw_input)
    except json.JSONDecodeError as exc:
        raise FileEditorToolError("File tool input must be valid JSON.") from exc

    if not isinstance(payload, dict):
        raise FileEditorToolError("File tool input must decode to a JSON object.")
    return payload


def _summarize_validation_error(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_input=False)
    details: list[str] = []
    for error in errors:
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "__root__")
        message = str(error.get("msg", "invalid value"))
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        details.append(f"{location}: {message}" if location else message)
    if not details:
        return "File tool input validation failed."
    return f"File tool input validation failed: {'; '.join(details)}"


def _parse_view_file_tool_input(raw_input: str) -> ViewFileToolRequest:
    try:
        return ViewFileToolRequest.model_validate(_parse_json_object(raw_input))
    except ValidationError as exc:
        raise FileEditorToolError(_summarize_validation_error(exc)) from exc


def _parse_search_file_tool_input(raw_input: str) -> SearchFileToolRequest:
    try:
        return SearchFileToolRequest.model_validate(_parse_json_object(raw_input))
    except ValidationError as exc:
        raise FileEditorToolError(_summarize_validation_error(exc)) from exc


def _parse_search_dir_tool_input(raw_input: str) -> SearchDirToolRequest:
    try:
        return SearchDirToolRequest.model_validate(_parse_json_object(raw_input))
    except ValidationError as exc:
        raise FileEditorToolError(_summarize_validation_error(exc)) from exc


def _parse_str_replace_editor_tool_input(raw_input: str) -> StrReplaceEditorToolRequest:
    try:
        return StrReplaceEditorToolRequest.model_validate(_parse_json_object(raw_input))
    except ValidationError as exc:
        raise FileEditorToolError(_summarize_validation_error(exc)) from exc


def _normalize_awk_path_argument(path: str) -> str:
    """Ensure awk treats simple relative paths as filenames, not assignments."""
    if path.startswith(("/", "./", "../")):
        return path
    if "=" in path or path.startswith("-"):
        return f"./{path}"
    return path


def build_view_file_command(request: ViewFileToolRequest) -> str:
    """Render a safe shell command for one normalized view-file request."""
    if request.start_line is None and request.end_line is None:
        program = r'{printf("%6d  %s\n", NR, $0)}'
    else:
        start_line = request.start_line or 1
        if request.end_line is None:
            program = rf'NR>={start_line} {{printf("%6d  %s\n", NR, $0)}}'
        else:
            program = (
                rf"NR>={start_line} && NR<={request.end_line} " r'{printf("%6d  %s\n", NR, $0)}'
            )
    return shlex.join(["awk", program, _normalize_awk_path_argument(request.path)])


def build_search_file_command(request: SearchFileToolRequest) -> str:
    """Render a safe shell command for one normalized search-file request."""
    tokens = [
        "rg",
        "--line-number",
        "--color=never",
        f"--context={request.context_lines}",
    ]
    if not request.regex:
        tokens.append("--fixed-strings")
    tokens.extend(["--", request.query, request.path])
    return shlex.join(tokens)


def build_search_dir_command(request: SearchDirToolRequest) -> str:
    """Render a safe shell command for one normalized search-dir request."""
    tokens = [
        "rg",
        "--line-number",
        "--color=never",
        f"--context={request.context_lines}",
    ]
    if not request.regex:
        tokens.append("--fixed-strings")
    tokens.extend(["--", request.query, request.path])
    return shlex.join(tokens)


def build_str_replace_editor_command(request: StrReplaceEditorToolRequest) -> str:
    """Render a safe shell command for one normalized exact-string replacement request."""
    check_program = (
        'BEGIN{old=ENVIRON["CODEX_OLD_TEXT"]; count=0} '
        "{line=$0; while((idx=index(line, old))>0){count++; line=substr(line, idx+1);}} "
        "END{"
        'if(count==0){print "str_replace_editor: old_text not found in file." '
        '> "/dev/stderr"; exit 3} '
        'if(count>1){print "str_replace_editor: old_text is ambiguous (matches '
        'multiple occurrences)." > "/dev/stderr"; exit 4}'
        "}"
    )
    replace_program = (
        'BEGIN{old=ENVIRON["CODEX_OLD_TEXT"]; new=ENVIRON["CODEX_NEW_TEXT"]; done=0} '
        "{line=$0; if(!done){idx=index(line, old); if(idx>0){line=substr(line,1,idx-1) "
        "new substr(line, idx+length(old)); done=1}} print line}"
    )
    path_arg = _normalize_awk_path_argument(request.path)
    tmp_suffix = hashlib.sha256(
        f"{request.path}\0{request.old_text}\0{request.new_text}".encode()
    ).hexdigest()[:12]
    tmp_path = f"{path_arg}.codex_tmp_replace_{tmp_suffix}"
    check_command = (
        f"CODEX_OLD_TEXT={shlex.quote(request.old_text)} "
        f"{shlex.join(['awk', check_program, path_arg])}"
    )
    replace_command = (
        f"CODEX_OLD_TEXT={shlex.quote(request.old_text)} "
        f"CODEX_NEW_TEXT={shlex.quote(request.new_text)} "
        f"{shlex.join(['awk', replace_program, path_arg])}"
    )
    write_command = f"{replace_command} > {shlex.quote(tmp_path)}"
    move_command = shlex.join(["mv", "--", tmp_path, request.path])
    cleanup_command = f"{shlex.join(['unlink', tmp_path])} 2>/dev/null || true"
    return (
        f"if {check_command} && {write_command} && {move_command}; "
        f"then true; else ret=$?; {cleanup_command}; exit $ret; fi"
    )


def build_view_file_command_from_input(raw_input: str) -> str:
    """Parse runtime tool input and render the corresponding view-file command."""
    return build_view_file_command(_parse_view_file_tool_input(raw_input))


def build_search_file_command_from_input(raw_input: str) -> str:
    """Parse runtime tool input and render the corresponding search-file command."""
    return build_search_file_command(_parse_search_file_tool_input(raw_input))


def build_search_dir_command_from_input(raw_input: str) -> str:
    """Parse runtime tool input and render the corresponding search-dir command."""
    return build_search_dir_command(_parse_search_dir_tool_input(raw_input))


def build_str_replace_editor_command_from_input(raw_input: str) -> str:
    """Parse runtime tool input and render the corresponding replacement command."""
    return build_str_replace_editor_command(_parse_str_replace_editor_tool_input(raw_input))
