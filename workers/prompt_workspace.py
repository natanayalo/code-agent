from pathlib import Path

from workers.adapter_utils import truncate_detail_keep_tail

DEFAULT_REPO_LISTING_MAX_DEPTH = 2
DEFAULT_REPO_LISTING_MAX_ENTRIES = 40
DEFAULT_AGENTS_MAX_CHARACTERS = 5000
DEFAULT_AGENTS_ASSET_READ_MAX_CHARACTERS = 8192

_TRUNCATED_MARKER = "\n... (truncated)"
_AGENTS_ASSET_DIRECTORIES = ("skills", "workflows", "agents")
_SKIPPED_PATH_NAMES = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".mypy_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)


def read_workspace_agents_guidance(
    workspace_path: Path,
    *,
    max_characters: int = DEFAULT_AGENTS_MAX_CHARACTERS,
) -> str | None:
    """Return bounded AGENTS.md guidance from the workspace root when present."""
    agents_path = workspace_path / "AGENTS.md"
    if not agents_path.is_file():
        return None

    contents = agents_path.read_text(encoding="utf-8", errors="replace").strip()
    if len(contents) <= max_characters:
        return contents
    truncated = contents[:max_characters].rstrip()
    return f"{truncated}\n... (truncated)"


def _truncate_to_budget(value: str, *, max_characters: int) -> str:
    """Truncate text while keeping the result length bounded."""
    if max_characters <= 0:
        return ""
    if len(value) <= max_characters:
        return value
    marker = _TRUNCATED_MARKER
    if len(marker) >= max_characters:
        return marker[:max_characters]
    available = max_characters - len(marker)
    return f"{value[:available].rstrip()}{marker}"


def _read_text_prefix(path: Path, *, max_characters: int) -> str:
    """Read up to a bounded number of characters from a text file."""
    if max_characters <= 0:
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as file_handle:
        return file_handle.read(max_characters)


def _extract_front_matter_metadata(contents: str) -> tuple[str | None, str | None, str]:
    """Extract markdown front matter name/description and return remaining body."""
    lines = contents.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None, contents

    closing_index = None
    for index in range(1, min(len(lines), 50)):
        if lines[index].strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        return None, None, contents

    name: str | None = None
    description: str | None = None
    for line in lines[1:closing_index]:
        key, separator, raw_value = line.partition(":")
        if separator != ":":
            continue
        normalized_key = key.strip().lower()
        value = raw_value.strip()
        if not value:
            continue
        if normalized_key == "name":
            name = value
        elif normalized_key == "description":
            description = value

    body = "\n".join(lines[closing_index + 1 :]).strip()
    return name, description, body


def _first_meaningful_line(contents: str) -> str | None:
    """Return the first non-empty content line useful for a compact summary."""
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            stripped = line.lstrip("#").strip()
            if stripped:
                return stripped
            continue
        return line
    return None


def _summarize_agents_asset(
    file_path: Path,
    *,
    category: str,
    relative_path: str,
) -> str | None:
    """Render one .agents markdown file into a concise prompt summary line."""
    try:
        contents = _read_text_prefix(
            file_path,
            max_characters=DEFAULT_AGENTS_ASSET_READ_MAX_CHARACTERS,
        ).strip()
    except OSError:
        return None
    if not contents:
        return None

    name, description, body = _extract_front_matter_metadata(contents)
    summary_name = name or file_path.stem
    summary_text = description or _first_meaningful_line(body)

    if summary_text:
        return f"- {category}/{relative_path}: {summary_name} - {summary_text}"
    return f"- {category}/{relative_path}: {summary_name}"


def read_workspace_agents_assets_guidance(
    workspace_path: Path,
    *,
    max_characters: int,
) -> str | None:
    """Return bounded summaries of markdown assets under .agents/."""
    if max_characters <= 0:
        return None

    lines: list[str] = []
    current_characters = 0
    exceeded_budget = False
    agents_root = workspace_path / ".agents"
    if not agents_root.is_dir():
        return None

    for category in _AGENTS_ASSET_DIRECTORIES:
        category_path = agents_root / category
        if not category_path.is_dir():
            continue
        for file_path in sorted(
            category_path.rglob("*.md"),
            key=lambda path: path.as_posix().lower(),
        ):
            if not file_path.is_file():
                continue
            relative_path = file_path.relative_to(category_path).as_posix()
            summary_line = _summarize_agents_asset(
                file_path,
                category=category,
                relative_path=relative_path,
            )
            if summary_line is None:
                continue
            lines.append(summary_line)
            if current_characters > 0:
                current_characters += 1
            current_characters += len(summary_line)
            if current_characters > max_characters:
                exceeded_budget = True
                break
        if exceeded_budget:
            break

    if not lines:
        return None
    return _truncate_to_budget("\n".join(lines), max_characters=max_characters)


def read_workspace_repo_guidance(
    workspace_path: Path,
    *,
    max_characters: int = DEFAULT_AGENTS_MAX_CHARACTERS,
) -> tuple[str | None, str | None]:
    """Return bounded AGENTS.md and .agents guidance within one shared budget."""
    if max_characters <= 0:
        return None, None

    agents_path = workspace_path / "AGENTS.md"
    agents_guidance: str | None = None
    remaining = max_characters

    if agents_path.is_file():
        try:
            agents_contents = _read_text_prefix(
                agents_path,
                max_characters=remaining + 1,
            ).strip()
        except OSError:
            agents_contents = ""
        if agents_contents:
            agents_guidance = truncate_detail_keep_tail(agents_contents, max_characters=remaining)
            remaining -= len(agents_guidance)

    agents_assets_guidance = read_workspace_agents_assets_guidance(
        workspace_path,
        max_characters=max(remaining, 0),
    )
    return agents_guidance, agents_assets_guidance


def build_workspace_directory_listing(
    workspace_path: Path,
    *,
    max_depth: int = DEFAULT_REPO_LISTING_MAX_DEPTH,
    max_entries: int = DEFAULT_REPO_LISTING_MAX_ENTRIES,
) -> str:
    """Build a deterministic bounded directory listing for prompt context."""
    root = workspace_path
    if not root.exists():
        return "<workspace path does not exist>"
    if not root.is_dir():
        return "<workspace path is not a directory>"

    entries: list[str] = []
    truncated = False

    def visit(path: Path, *, depth: int) -> None:
        nonlocal truncated
        if truncated or depth >= max_depth:
            return

        try:
            children = sorted(
                (child for child in path.iterdir() if child.name not in _SKIPPED_PATH_NAMES),
                key=lambda child: (not child.is_dir(), child.name.lower()),
            )
        except OSError:
            return
        for child in children:
            if len(entries) >= max_entries:
                truncated = True
                return
            relative_path = child.relative_to(root).as_posix()
            entries.append(f"{relative_path}/" if child.is_dir() else relative_path)
            if child.is_dir():
                visit(child, depth=depth + 1)

    visit(root, depth=0)

    if not entries:
        return "<workspace is empty>"
    if truncated:
        entries.append("... (truncated)")
    return "\n".join(entries)


def build_repo_context_section(workspace_path: Path) -> str:
    """Render repo-level prompt context from the workspace."""
    return _build_repo_context_section_with_guidance(workspace_path)


def _build_repo_context_section_with_guidance(
    workspace_path: Path,
    *,
    omit_dir_listing: bool = False,
) -> str:
    """Render the repository context section with directory tree."""
    lines = ["## Repo Context"]
    if not omit_dir_listing:
        lines.append("Directory listing:")
        lines.append(f"```text\n{build_workspace_directory_listing(workspace_path)}\n```")
    return "\n".join(lines)
