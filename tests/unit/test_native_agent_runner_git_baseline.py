"""Regression tests for native-agent git baseline accounting."""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from sandbox.native_agent_executor import NativeAgentExecution, NativeAgentExecutorError
from workers.native_agent_read_only import (
    ReadOnlySnapshotError,
    capture_read_only_workspace_snapshot,
)
from workers.native_agent_runner import NativeAgentRunRequest, run_native_agent


def _write_fake_binary(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _init_git_repo(repo_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test Runner"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (repo_path / ".seed").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_native_agent_runner_collects_committed_changes_from_start_ref(tmp_path: Path) -> None:
    """Committed native-agent edits should still be reported after a clean working tree."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)

    fake_binary = _write_fake_binary(
        tmp_path / "fake-commit-agent.py",
        """#!/usr/bin/env python3
import subprocess
from pathlib import Path

Path("committed.txt").write_text("committed by native agent\\n", encoding="utf-8")
subprocess.run(["git", "add", "committed.txt"], check=True)
subprocess.run(["git", "commit", "-m", "agent change"], check=True)
print("committed change")
""",
    )

    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="commit the task result",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=10,
        )
    )

    assert result.status == "success"
    assert result.files_changed == ["committed.txt"]
    assert result.diff_text is not None
    assert "diff --git a/committed.txt b/committed.txt" in result.diff_text
    assert "committed by native agent" in result.diff_text
    assert "native-agent-diff" in {artifact.name for artifact in result.artifacts}


def test_collect_diff_text_since_ref_omits_head_without_base_ref(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Unborn repositories should not receive HEAD when no baseline ref exists."""
    calls: list[list[str]] = []

    def _fake_run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    from workers.native_agent_artifacts import _collect_diff_text_since_ref

    assert (
        _collect_diff_text_since_ref(
            repo_path=tmp_path,
            base_ref=None,
            timeout_seconds=10,
        )
        is None
    )
    assert calls == [["git", "-C", str(tmp_path), "diff", "--no-color", "--", "."]]


def _read_only_request(
    binary: Path, repo_path: Path, workspace_path: Path
) -> NativeAgentRunRequest:
    return NativeAgentRunRequest(
        command=[str(binary)],
        prompt="inspect only",
        repo_path=repo_path,
        workspace_path=workspace_path,
        timeout_seconds=10,
        read_only_workspace=True,
    )


def test_read_only_runner_ignores_inherited_source_edits_but_keeps_task_diff_artifact(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    (repo_path / ".seed").write_text("inherited tracked edit\n", encoding="utf-8")
    (repo_path / "inherited-untracked.txt").write_text("inherited\n", encoding="utf-8")
    binary = _write_fake_binary(
        tmp_path / "read-only-noop.py",
        "#!/usr/bin/env python3\nprint('inspection complete')\n",
    )

    result = run_native_agent(_read_only_request(binary, repo_path, tmp_path))

    assert result.status == "success"
    assert result.files_changed == []
    assert result.diff_text is None
    diff_artifact = next(
        artifact for artifact in result.artifacts if artifact.name == "native-agent-diff"
    )
    assert "inherited tracked edit" in Path(diff_artifact.uri.removeprefix("file://")).read_text()


@pytest.mark.parametrize(
    ("name", "before", "agent_body", "expected_path"),
    [
        (
            "modifies-existing-dirty-file",
            {"dirty.txt": "inherited\n"},
            "Path('dirty.txt').write_text('changed by verifier\\n')",
            "dirty.txt",
        ),
        (
            "adds-file",
            {},
            "Path('added.txt').write_text('new file\\n')",
            "added.txt",
        ),
        (
            "deletes-file",
            {"deleted.txt": "remove me\n"},
            "Path('deleted.txt').unlink()",
            "deleted.txt",
        ),
        (
            "renames-file",
            {"before.txt": "rename me\n"},
            "Path('before.txt').rename('after.txt')",
            "after.txt",
        ),
        (
            "changes-mode",
            {"mode.txt": "mode\n"},
            "Path('mode.txt').chmod(0o755)",
            "mode.txt",
        ),
    ],
)
def test_read_only_runner_rejects_invocation_scoped_mutations(
    tmp_path: Path,
    name: str,
    before: dict[str, str],
    agent_body: str,
    expected_path: str,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    for path, content in before.items():
        (repo_path / path).write_text(content, encoding="utf-8")
    binary = _write_fake_binary(
        tmp_path / f"read-only-{name}.py",
        f"""#!/usr/bin/env python3
from pathlib import Path
{agent_body}
print("inspection complete")
""",
    )

    result = run_native_agent(_read_only_request(binary, repo_path, tmp_path))

    assert result.status == "failure"
    assert result.summary == "READ_ONLY_VIOLATION: native executor mutated the workspace."
    assert expected_path in result.files_changed
    assert result.diff_text is not None
    assert "READ_ONLY_MUTATION_EVIDENCE" in result.diff_text


def test_read_only_snapshot_records_source_metadata_and_excludes_runner_paths(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".gitignore").write_text("ignored-source.txt\n", encoding="utf-8")
    (repo_path / "ignored-source.txt").write_text("source\n", encoding="utf-8")
    source_dir = repo_path / "source-dir"
    source_dir.mkdir()
    source_dir.chmod(0o750)
    (repo_path / "link").symlink_to("ignored-source.txt")
    (repo_path / ".code-agent").mkdir()
    (repo_path / ".code-agent" / "runner.log").write_text("runtime\n", encoding="utf-8")
    (repo_path / ".venv").mkdir()
    (repo_path / ".venv" / "runtime.py").write_text("runtime\n", encoding="utf-8")
    nested_runtime = repo_path / "package" / "__pycache__"
    nested_runtime.mkdir(parents=True)
    (nested_runtime / "cached.pyc").write_bytes(b"runtime")

    snapshot = capture_read_only_workspace_snapshot(repo_path)

    assert snapshot["ignored-source.txt"][0] == "file"
    assert snapshot["source-dir"][:2] == ("directory", 0o750)
    assert snapshot["link"] == ("symlink", snapshot["link"][1], "ignored-source.txt")
    assert ".code-agent/runner.log" not in snapshot
    assert ".venv/runtime.py" not in snapshot
    assert "package/__pycache__/cached.pyc" not in snapshot


def test_read_only_snapshot_fails_closed_for_missing_unreadable_and_special_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with pytest.raises(ReadOnlySnapshotError, match="repository path is unavailable"):
        capture_read_only_workspace_snapshot(tmp_path / "missing")

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    blocked = repo_path / "blocked.txt"
    blocked.write_text("content\n", encoding="utf-8")
    with patch(
        "workers.native_agent_read_only._snapshot_entry",
        side_effect=PermissionError("denied"),
    ):
        with pytest.raises(ReadOnlySnapshotError, match="cannot inspect"):
            capture_read_only_workspace_snapshot(repo_path)

    os.mkfifo(repo_path / "unsupported.fifo")
    with pytest.raises(ReadOnlySnapshotError, match="unsupported special entry"):
        capture_read_only_workspace_snapshot(repo_path)


def test_read_only_runner_fails_closed_when_snapshot_is_unavailable_before_or_after_run(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    binary = _write_fake_binary(
        tmp_path / "read-only-success.py",
        "#!/usr/bin/env python3\nprint('inspection complete')\n",
    )
    request = _read_only_request(binary, repo_path, tmp_path)

    with patch(
        "workers.native_agent_runner.capture_read_only_workspace_snapshot",
        side_effect=ReadOnlySnapshotError("before unavailable"),
    ):
        before_failure = run_native_agent(request)
    assert before_failure.status == "error"
    assert before_failure.summary.startswith("READ_ONLY_SNAPSHOT_UNAVAILABLE")

    with patch(
        "workers.native_agent_runner.capture_read_only_workspace_snapshot",
        side_effect=[{}, ReadOnlySnapshotError("after unavailable")],
    ):
        after_failure = run_native_agent(request)
    assert after_failure.status == "error"
    assert after_failure.summary.startswith("READ_ONLY_SNAPSHOT_UNAVAILABLE")


@pytest.mark.parametrize("outcome", ["startup_error", "timeout"])
def test_read_only_runner_checks_workspace_after_early_process_outcomes(
    tmp_path: Path,
    outcome: str,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)

    class MutatingRunner:
        def run(self, **_kwargs):
            (repo_path / f"{outcome}.txt").write_text("mutated\n", encoding="utf-8")
            if outcome == "startup_error":
                raise NativeAgentExecutorError("executor unavailable")
            return NativeAgentExecution(
                completed=subprocess.CompletedProcess([], 1, stdout="", stderr=""),
                termination_reason="timeout",
                manifest_path=tmp_path / "manifest.json",
            )

    result = run_native_agent(
        NativeAgentRunRequest(
            command=["ignored"],
            prompt="inspect only",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=1,
            read_only_workspace=True,
            process_runner=MutatingRunner(),
        )
    )

    assert result.status == "failure"
    assert result.summary == "READ_ONLY_VIOLATION: native executor mutated the workspace."
    assert result.files_changed == [f"{outcome}.txt"]
