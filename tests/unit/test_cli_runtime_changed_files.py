# ruff: noqa: F403, F405
"""Behavior-focused CLI runtime tests."""

from __future__ import annotations

from tests.unit.cli_runtime_support import *  # noqa: F403


def test_collect_changed_files_parses_modified_renamed_and_untracked_paths() -> None:
    """Changed file collection should normalize the common porcelain shapes we rely on."""
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": _command_result(
                "git status --porcelain=v1 -z --untracked-files=all",
                output=" M README.md\0R  new.py\0old.py\0?? tests/test_new.py\0",
            )
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == ["README.md", "new.py", "tests/test_new.py"]


def test_collect_changed_files_does_not_treat_non_rename_arrow_paths_as_renames() -> None:
    """NUL-delimited porcelain output should preserve literal arrows in ordinary paths."""
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": _command_result(
                "git status --porcelain=v1 -z --untracked-files=all",
                output=" M docs/name -> value.txt\0?? tests/path -> sample.txt\0",
            )
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == ["docs/name -> value.txt", "tests/path -> sample.txt"]


def test_collect_changed_files_handles_rename_paths_and_newlines_with_porcelain_z() -> None:
    """Porcelain -z output should preserve literal rename and newline characters in paths."""
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": _command_result(
                "git status --porcelain=v1 -z --untracked-files=all",
                output="R  new -> name.txt\0old -> name.txt\0?? line\nbreak.txt\0",
            )
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == ["new -> name.txt", "line\nbreak.txt"]


def test_collect_changed_files_falls_back_when_porcelain_z_raises() -> None:
    """When porcelain -z execution fails, fallback line parsing should still report files."""
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": DockerShellSessionError("boom"),
            "git status --porcelain=v1 --untracked-files=all": _command_result(
                "git status --porcelain=v1 --untracked-files=all",
                output="?? hello_runtime.txt\n M README.md\nR  old_name.py -> new_name.py\n",
            ),
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == ["hello_runtime.txt", "README.md", "new_name.py"]
    assert [command for command, _ in session.calls] == [
        "git status --porcelain=v1 -z --untracked-files=all",
        "git status --porcelain=v1 --untracked-files=all",
    ]


def test_collect_changed_files_falls_back_when_porcelain_z_exits_non_zero() -> None:
    """When porcelain -z returns non-zero, fallback line parsing should be attempted."""
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": _command_result(
                "git status --porcelain=v1 -z --untracked-files=all",
                output="fatal: unsupported option\n",
                exit_code=129,
            ),
            "git status --porcelain=v1 --untracked-files=all": _command_result(
                "git status --porcelain=v1 --untracked-files=all",
                output="?? note.txt\n",
            ),
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == ["note.txt"]
    assert [command for command, _ in session.calls] == [
        "git status --porcelain=v1 -z --untracked-files=all",
        "git status --porcelain=v1 --untracked-files=all",
    ]


def test_collect_changed_files_returns_empty_when_workspace_is_not_a_git_repo() -> None:
    """Non-repository workspaces should short-circuit changed-file collection quietly."""
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": _command_result(
                "git status --porcelain=v1 -z --untracked-files=all",
                output="fatal: not a git repository (or any of the parent directories): .git\n",
                exit_code=128,
            ),
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == []
    assert [command for command, _ in session.calls] == [
        "git status --porcelain=v1 -z --untracked-files=all"
    ]


def test_collect_changed_files_runs_git_in_explicit_working_directory() -> None:
    """Changed file collection should target the repo path when provided."""
    repo_path = Path("/workspace/repo")
    status_command = "git -C /workspace/repo status --porcelain=v1 -z --untracked-files=all"
    session = _FakeSession(
        {
            status_command: _command_result(
                status_command,
                output="?? runtime_fix_probe.txt\0",
            )
        }
    )

    changed_files = collect_changed_files(session, working_directory=repo_path)

    assert changed_files == ["runtime_fix_probe.txt"]
    assert [command for command, _ in session.calls] == [status_command]


def test_collect_changed_files_from_repo_path_parses_porcelain_z_output(monkeypatch) -> None:
    """Host-side fallback should parse git porcelain output for changed files."""

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=b" M README.md\0?? runtime_ok_2.txt\0",
            stderr=b"",
        )

    monkeypatch.setattr("workers.cli_runtime_files.subprocess.run", _fake_run)

    changed_files = collect_changed_files_from_repo_path(Path("/tmp/repo"))

    assert changed_files == ["README.md", "runtime_ok_2.txt"]


def test_collect_changed_files_from_repo_path_ignores_only_untracked_native_paths(
    monkeypatch,
) -> None:
    """Keep tracked repository paths even when their names look internal."""

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=(
                b" M README.md\0"
                b" M .vscode/settings.json\0"
                b" M .agent_home/.gemini/settings.json\0"
                b"?? .agent_home/.gemini/antigravity-cli/cache.json\0"
                b"?? .code-agent/native-agent-runner/stdout.txt\0"
            ),
            stderr=b"",
        )

    monkeypatch.setattr("workers.cli_runtime_files.subprocess.run", _fake_run)

    changed_files = collect_changed_files_from_repo_path(Path("/tmp/repo"))

    assert changed_files == [
        "README.md",
        ".vscode/settings.json",
        ".agent_home/.gemini/settings.json",
    ]


def test_collect_changed_files_from_repo_path_preserves_tracked_renames_and_copies(
    monkeypatch,
) -> None:
    """Keep tracked rename and copy destinations in changed-file evidence."""

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=(
                b"R  .agent_home/.gemini/settings.json\0old-settings.json\0"
                b"C  .vscode/copied-settings.json\0source-settings.json\0"
            ),
            stderr=b"",
        )

    monkeypatch.setattr("workers.cli_runtime_files.subprocess.run", _fake_run)

    changed_files = collect_changed_files_from_repo_path(Path("/tmp/repo"))

    assert changed_files == [
        ".agent_home/.gemini/settings.json",
        ".vscode/copied-settings.json",
    ]


def test_collect_changed_files_since_ref_preserves_all_baseline_paths(monkeypatch) -> None:
    """Baseline diffs preserve all paths, including internal-looking names."""

    def _fake_run(args, **_kwargs):
        if "status" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=(
                b"README.md\0"
                b".agent_home/.gemini/settings.json\0"
                b".vscode/copied-settings.json\0"
            ),
            stderr=b"",
        )

    monkeypatch.setattr("workers.cli_runtime_files.subprocess.run", _fake_run)

    changed_files = collect_changed_files_since_ref_from_repo_path(
        Path("/tmp/repo"),
        base_ref="base-ref",
    )

    assert changed_files == [
        "README.md",
        ".agent_home/.gemini/settings.json",
        ".vscode/copied-settings.json",
    ]


def test_collect_changed_files_from_repo_path_logs_timeout_details(monkeypatch) -> None:
    """Host fallback should log timeout details when git status exceeds timeout."""

    def _fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    warning_calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr("workers.cli_runtime_files.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "workers.cli_runtime_files.logger.warning",
        lambda message, **kwargs: warning_calls.append((message, kwargs)),
    )

    changed_files = collect_changed_files_from_repo_path(Path("/tmp/repo"), timeout_seconds=7)

    assert changed_files == []
    assert warning_calls
    assert "timed out" in warning_calls[0][0].lower()
    assert warning_calls[0][1]["extra"] == {"timeout_seconds": 7}
    assert warning_calls[0][1]["exc_info"] is not None


def test_collect_changed_files_since_ref_truncates_git_failure_output(monkeypatch) -> None:
    """Baseline git diff failures should log bounded decoded output."""

    calls: list[list[str]] = []

    def _fake_run(args, **_kwargs):
        calls.append(args)
        if "status" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=b" M working.txt\0",
                stderr=b"",
            )
        return subprocess.CompletedProcess(
            args=args,
            returncode=128,
            stdout=b"stdout-prefix",
            stderr=("x" * 3000 + "stderr-tail").encode(),
        )

    warning_calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr("workers.cli_runtime_files.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "workers.cli_runtime_files.logger.warning",
        lambda message, **kwargs: warning_calls.append((message, kwargs)),
    )

    changed_files = collect_changed_files_since_ref_from_repo_path(
        Path("/tmp/repo"),
        base_ref="base-ref",
    )

    assert changed_files == ["working.txt"]
    assert calls[1][:6] == ["git", "-C", "/tmp/repo", "diff", "--name-only", "-z"]
    assert warning_calls
    output = warning_calls[0][1]["extra"]["output"]
    assert isinstance(output, str)
    assert output.startswith("[truncated]...")
    assert output.endswith("stderr-tail")
