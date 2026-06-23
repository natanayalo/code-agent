from __future__ import annotations

from sandbox.audit import _should_ignore_path, parse_git_status_entries


def test_should_ignore_path() -> None:
    # Artifacts should be ignored
    assert _should_ignore_path("artifacts/command-123/stdout.log") is True
    assert _should_ignore_path("artifacts") is True

    # Common noise segments should be ignored
    assert _should_ignore_path(".cache/pypoetry/something.whl") is True
    assert _should_ignore_path("src/__pycache__/main.cpython-312.pyc") is True
    assert _should_ignore_path(".pytest_cache/v/cache/lastfailed") is True
    assert _should_ignore_path(".venv/bin/python") is True
    assert _should_ignore_path(".git/HEAD") is True
    assert _should_ignore_path("node_modules/package.json") is True
    assert _should_ignore_path(".DS_Store") is True

    # Native agent scratch dirs must be ignored (scope_mismatch false-positive fix)
    assert _should_ignore_path(".agent_home/.cache/ms-playwright-go/1.57.0/package/cli.js") is True
    assert _should_ignore_path(".agent_home/.gemini/antigravity-cli/cli.log") is True
    assert _should_ignore_path(".code-agent/native-agent-runner/run-123/stdout.txt") is True

    # Meaningful files should NOT be ignored
    assert _should_ignore_path("src/main.py") is False
    assert _should_ignore_path("tests/unit/test_cli_runtime.py") is False
    assert _should_ignore_path("README.md") is False
    assert _should_ignore_path("pyproject.toml") is False
    assert (
        _should_ignore_path("cache_manager.py") is False
    )  # contains "cache" but as part of name, not segment


def test_parse_git_status_entries_with_renames() -> None:
    # Porcelain v1 -z output uses \0 separators
    # Renames are R  old\0new\0
    output = "M  tracked.py\0?? untracked.py\0R  old.py\0new.py\0"
    entries = parse_git_status_entries(output)

    assert len(entries) == 3
    assert entries[0] == ("M ", "tracked.py")
    assert entries[1] == ("??", "untracked.py")
    assert entries[2] == ("R ", "new.py")
