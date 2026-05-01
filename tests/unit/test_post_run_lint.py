"""Unit tests for post-run lint/format helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox import DockerShellCommandResult
from sandbox.workspace import SandboxArtifact
from workers.post_run_lint import (
    apply_post_run_lint_format,
    collect_changed_files_and_apply_post_run_lint_format,
    detect_post_run_lint_commands,
    merge_post_run_lint_results,
    run_post_run_lint,
)


class _FakeSession:
    def __init__(self, responses: dict[str, DockerShellCommandResult]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, int]] = []

    def execute(self, command: str, *, timeout_seconds: int = 300) -> DockerShellCommandResult:
        self.calls.append((command, timeout_seconds))
        return self._responses[command]

    def close(self) -> None:
        return None


def test_detect_post_run_lint_commands_prefers_ruff_with_python_changes(tmp_path: Path) -> None:
    """Ruff lint/format should be selected when pyproject config is present."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")

    commands = detect_post_run_lint_commands(
        repo_path=tmp_path,
        files_changed=["workers/codex_cli_worker.py", "README.md"],
    )

    assert commands == [
        "ruff format -- workers/codex_cli_worker.py",
        "ruff check --fix -- workers/codex_cli_worker.py",
    ]


def test_detect_post_run_lint_commands_uses_fallback_template(tmp_path: Path) -> None:
    """Fallback templates should expand the {files} placeholder."""
    commands = detect_post_run_lint_commands(
        repo_path=tmp_path,
        files_changed=["a.py", "b.py"],
        fallback_command_template="custom-fmt {files}",
    )

    assert commands == ["custom-fmt a.py b.py"]


def test_detect_post_run_lint_commands_uses_package_json_scripts(tmp_path: Path) -> None:
    """Package script detection should prefer format + lint flows with file args."""
    (tmp_path / "package.json").write_text(
        ('{"scripts":{"format":"prettier --write .","lint":"eslint .","test":"vitest run"}}'),
        encoding="utf-8",
    )

    commands = detect_post_run_lint_commands(
        repo_path=tmp_path,
        files_changed=["src/app.ts", "src/app.test.ts"],
    )

    assert commands == [
        "npm run format -- src/app.ts src/app.test.ts",
        "npm run lint -- --fix src/app.ts src/app.test.ts",
    ]


def test_detect_post_run_lint_commands_uses_makefile_targets(tmp_path: Path) -> None:
    """Makefile target detection should emit file-scoped format + lint commands."""
    (tmp_path / "Makefile").write_text(
        "\n".join(
            [
                ".PHONY: lint format test",
                "format:",
                "\t@echo formatting",
                "lint:",
                "\t@echo linting",
            ]
        ),
        encoding="utf-8",
    )

    commands = detect_post_run_lint_commands(
        repo_path=tmp_path,
        files_changed=["workers/codex_cli_worker.py", "workers/gemini_cli_worker.py"],
    )

    assert commands == [
        "make format FILES='workers/codex_cli_worker.py workers/gemini_cli_worker.py'",
        "make lint FILES='workers/codex_cli_worker.py workers/gemini_cli_worker.py'",
    ]


def test_run_post_run_lint_captures_commands_artifacts_and_errors(tmp_path: Path) -> None:
    """Execution metadata should include artifacts and non-zero exit warnings."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n", encoding="utf-8")
    repo_dir = Path("/workspace/repo")
    format_command = "cd /workspace/repo && ruff format -- workers/codex_cli_worker.py"
    check_command = "cd /workspace/repo && ruff check --fix -- workers/codex_cli_worker.py"
    session = _FakeSession(
        {
            format_command: DockerShellCommandResult(
                command=format_command,
                output="formatted",
                exit_code=0,
                duration_seconds=0.1,
                artifacts=[
                    SandboxArtifact(
                        name="stdout.log",
                        uri="artifacts/run-1/stdout.log",
                        artifact_type="log",
                        artifact_metadata={"stream": "stdout"},
                    ),
                    SandboxArtifact(
                        name="stderr.log",
                        uri="artifacts/run-1/stderr.log",
                        artifact_type="log",
                        artifact_metadata={"stream": "stderr"},
                    ),
                ],
            ),
            check_command: DockerShellCommandResult(
                command=check_command,
                output="lint errors",
                exit_code=1,
                duration_seconds=0.2,
                artifacts=[],
            ),
        }
    )

    result = run_post_run_lint(
        session=session,
        repo_path_for_detection=tmp_path,
        repo_working_directory=repo_dir,
        files_changed=["workers/codex_cli_worker.py"],
        timeout_seconds=12,
    )

    assert result["ran"] is True
    assert result["status"] == "warning"
    assert len(result["commands"]) == 2
    assert result["commands"][0]["command"] == "ruff format -- workers/codex_cli_worker.py"
    assert result["commands"][0]["stdout_artifact_uri"] == "artifacts/run-1/stdout.log"
    assert result["commands"][0]["stderr_artifact_uri"] == "artifacts/run-1/stderr.log"
    assert result["commands"][1]["exit_code"] == 1
    assert len(result["artifacts"]) == 2
    assert result["errors"] == [
        "`ruff check --fix -- workers/codex_cli_worker.py` exited with status 1"
    ]
    assert session.calls == [(format_command, 12), (check_command, 12)]


def test_run_post_run_lint_skips_when_no_command_detected(tmp_path: Path) -> None:
    """Repos without known tooling should skip cleanly."""
    session = _FakeSession({})

    result = run_post_run_lint(
        session=session,
        repo_path_for_detection=tmp_path,
        repo_working_directory=tmp_path,
        files_changed=["README.md"],
        timeout_seconds=3,
    )

    assert result == {
        "ran": False,
        "status": "skipped",
        "reason": "no_detected_lint_or_format_command",
        "commands": [],
        "errors": [],
        "artifacts": [],
    }
    assert session.calls == []


def test_run_post_run_lint_emits_manual_span(monkeypatch, tmp_path: Path) -> None:
    """Post-run lint should emit an OTEL span when tracing deps are installed."""
    session = _FakeSession({})
    span_events: list[dict[str, object]] = []

    class _FakeSpan:
        def __init__(self, name: str, attributes: dict[str, object] | None) -> None:
            self.name = name
            self.attributes = dict(attributes or {})
            self.set_attributes: dict[str, object] = {}

        def __enter__(self):
            span_events.append(
                {
                    "name": self.name,
                    "attributes": self.attributes,
                    "set_attributes": self.set_attributes,
                }
            )
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        def set_attribute(self, key: str, value: object) -> None:
            self.set_attributes[key] = value

    class _FakeTracer:
        def start_as_current_span(self, name: str, attributes: dict[str, object] | None = None):
            return _FakeSpan(name, attributes)

    class _FakeTraceApi:
        def get_tracer(self, name: str):
            assert name == "workers.post_run_lint"
            return _FakeTracer()

    monkeypatch.setitem(sys.modules, "opentelemetry", SimpleNamespace(trace=_FakeTraceApi()))

    result = run_post_run_lint(
        session=session,
        repo_path_for_detection=tmp_path,
        repo_working_directory=tmp_path,
        files_changed=["README.md"],
        timeout_seconds=3,
    )

    assert result["status"] == "skipped"
    assert span_events
    assert span_events[0]["name"] == "worker.post_run_lint"
    assert span_events[0]["attributes"]["code_agent.files_changed_count"] == 1
    assert span_events[0]["set_attributes"]["code_agent.commands_detected_count"] == 0


def test_merge_post_run_lint_results_combines_metadata_across_passes() -> None:
    """Multiple lint passes should retain a combined metadata trail."""
    merged = merge_post_run_lint_results(
        {
            "ran": True,
            "status": "passed",
            "reason": None,
            "commands": [{"command": "ruff format -- a.py"}],
            "errors": [],
            "artifacts": [{"name": "first.log"}],
        },
        {
            "ran": True,
            "status": "warning",
            "reason": None,
            "commands": [{"command": "ruff check --fix -- a.py"}],
            "errors": ["`ruff check --fix -- a.py` exited with status 1"],
            "artifacts": [{"name": "second.log"}],
        },
    )

    assert merged["ran"] is True
    assert merged["status"] == "warning"
    assert merged["commands"] == [
        {"command": "ruff format -- a.py"},
        {"command": "ruff check --fix -- a.py"},
    ]
    assert merged["reason"] is None
    assert merged["errors"] == ["`ruff check --fix -- a.py` exited with status 1"]
    assert merged["artifacts"] == [{"name": "first.log"}, {"name": "second.log"}]


def test_collect_and_lint_skips_collection_when_changed_files_not_expected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changed-file collection should be skipped when tool metadata does not expect it."""
    session = _FakeSession({})
    execution = SimpleNamespace(commands_run=[])

    def _unexpected_collect(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("collect_changed_files should not run")

    def _unexpected_fallback(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("collect_changed_files_from_repo_path should not run")

    monkeypatch.setattr("workers.post_run_lint.collect_changed_files", _unexpected_collect)
    monkeypatch.setattr(
        "workers.post_run_lint.collect_changed_files_from_repo_path",
        _unexpected_fallback,
    )

    files_changed, lint_result, lint_artifacts = (
        collect_changed_files_and_apply_post_run_lint_format(
            session=session,
            execution=execution,
            expect_changed_files_artifact=False,
            repo_path_for_detection=tmp_path,
            repo_working_directory=tmp_path,
            timeout_seconds=5,
            existing_files_changed=["README.md"],
        )
    )

    assert files_changed == ["README.md"]
    assert lint_result["status"] == "skipped"
    assert lint_artifacts == []
    assert session.calls == []


def test_collect_and_lint_uses_repo_path_fallback_when_session_collect_returns_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host git fallback should seed post-run lint when session-side collection is empty."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n", encoding="utf-8")
    repo_dir = Path("/workspace/repo")
    format_command = "cd /workspace/repo && ruff format -- workers/codex_cli_worker.py"
    check_command = "cd /workspace/repo && ruff check --fix -- workers/codex_cli_worker.py"
    session = _FakeSession(
        {
            format_command: DockerShellCommandResult(
                command=format_command,
                output="formatted",
                exit_code=0,
                duration_seconds=0.1,
            ),
            check_command: DockerShellCommandResult(
                command=check_command,
                output="checked",
                exit_code=0,
                duration_seconds=0.1,
            ),
        }
    )
    execution = SimpleNamespace(commands_run=[])

    monkeypatch.setattr("workers.post_run_lint.collect_changed_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "workers.post_run_lint.collect_changed_files_from_repo_path",
        lambda *args, **kwargs: ["workers/codex_cli_worker.py"],
    )

    files_changed, lint_result, lint_artifacts = (
        collect_changed_files_and_apply_post_run_lint_format(
            session=session,
            execution=execution,
            expect_changed_files_artifact=True,
            repo_path_for_detection=tmp_path,
            repo_working_directory=repo_dir,
            timeout_seconds=8,
            existing_files_changed=["README.md"],
        )
    )

    assert files_changed == ["README.md", "workers/codex_cli_worker.py"]
    assert lint_result["ran"] is True
    assert lint_result["status"] == "passed"
    assert len(execution.commands_run) == 2
    assert len(lint_artifacts) == 0
    assert session.calls == [(format_command, 8), (check_command, 8)]


def test_collect_and_lint_preserves_existing_files_when_collectors_return_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty recollection should not discard previously known changed files."""
    session = _FakeSession({})
    execution = SimpleNamespace(commands_run=[])

    monkeypatch.setattr("workers.post_run_lint.collect_changed_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "workers.post_run_lint.collect_changed_files_from_repo_path",
        lambda *args, **kwargs: [],
    )

    files_changed, lint_result, lint_artifacts = (
        collect_changed_files_and_apply_post_run_lint_format(
            session=session,
            execution=execution,
            expect_changed_files_artifact=True,
            repo_path_for_detection=tmp_path,
            repo_working_directory=tmp_path,
            timeout_seconds=8,
            existing_files_changed=["workers/codex_cli_worker.py"],
        )
    )

    assert files_changed == ["workers/codex_cli_worker.py"]
    assert lint_result["status"] == "skipped"
    assert lint_artifacts == []
    assert session.calls == []


def test_apply_post_run_lint_refresh_uses_fallback_when_session_collect_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-lint refresh should fall back to host git status when session collection is empty."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    repo_dir = Path("/workspace/repo")
    format_command = "cd /workspace/repo && ruff format -- workers/codex_cli_worker.py"
    check_command = "cd /workspace/repo && ruff check --fix -- workers/codex_cli_worker.py"
    session = _FakeSession(
        {
            format_command: DockerShellCommandResult(
                command=format_command,
                output="formatted",
                exit_code=0,
                duration_seconds=0.1,
            ),
            check_command: DockerShellCommandResult(
                command=check_command,
                output="checked",
                exit_code=0,
                duration_seconds=0.1,
            ),
        }
    )
    execution = SimpleNamespace(commands_run=[])

    monkeypatch.setattr("workers.post_run_lint.collect_changed_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "workers.post_run_lint.collect_changed_files_from_repo_path",
        lambda *args, **kwargs: ["workers/codex_cli_worker.py", "workers/gemini_cli_worker.py"],
    )

    files_changed, lint_result, lint_artifacts = apply_post_run_lint_format(
        session=session,
        execution=execution,
        files_changed=["README.md", "workers/codex_cli_worker.py"],
        repo_path_for_detection=tmp_path,
        repo_working_directory=repo_dir,
        timeout_seconds=8,
    )

    assert files_changed == [
        "README.md",
        "workers/codex_cli_worker.py",
        "workers/gemini_cli_worker.py",
    ]
    assert lint_result["status"] == "passed"
    assert len(lint_artifacts) == 0


def test_apply_post_run_lint_refresh_preserves_existing_files_when_collectors_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-lint refresh should keep prior changed files when both collectors return empty."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    repo_dir = Path("/workspace/repo")
    format_command = "cd /workspace/repo && ruff format -- workers/codex_cli_worker.py"
    check_command = "cd /workspace/repo && ruff check --fix -- workers/codex_cli_worker.py"
    session = _FakeSession(
        {
            format_command: DockerShellCommandResult(
                command=format_command,
                output="formatted",
                exit_code=0,
                duration_seconds=0.1,
            ),
            check_command: DockerShellCommandResult(
                command=check_command,
                output="checked",
                exit_code=0,
                duration_seconds=0.1,
            ),
        }
    )
    execution = SimpleNamespace(commands_run=[])

    monkeypatch.setattr("workers.post_run_lint.collect_changed_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "workers.post_run_lint.collect_changed_files_from_repo_path",
        lambda *args, **kwargs: [],
    )

    files_changed, lint_result, lint_artifacts = apply_post_run_lint_format(
        session=session,
        execution=execution,
        files_changed=["workers/codex_cli_worker.py"],
        repo_path_for_detection=tmp_path,
        repo_working_directory=repo_dir,
        timeout_seconds=8,
    )

    assert files_changed == ["workers/codex_cli_worker.py"]
    assert lint_result["status"] == "passed"
    assert len(lint_artifacts) == 0
