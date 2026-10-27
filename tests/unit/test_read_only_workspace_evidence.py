"""Empty legacy diff placeholders require independent clean Git evidence."""

import subprocess
from types import SimpleNamespace

from orchestrator.read_only_evidence import has_clean_trusted_workspace


def test_clean_workspace_is_verified_and_untracked_changes_fail(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    worker = SimpleNamespace(workspace_manager=SimpleNamespace(root_dir=tmp_path))
    assert has_clean_trusted_workspace(worker, "workspace")
    (workspace / "new-file").write_text("changed")
    assert not has_clean_trusted_workspace(worker, "workspace")


def test_missing_and_escaping_workspaces_fail_closed(tmp_path):
    worker = SimpleNamespace(workspace_manager=SimpleNamespace(root_dir=tmp_path))
    assert not has_clean_trusted_workspace(worker, None)
    assert not has_clean_trusted_workspace(worker, "../escape")
    assert not has_clean_trusted_workspace(worker, "missing")
    assert not has_clean_trusted_workspace(object(), "workspace")


def test_unavailable_or_timed_out_git_fails_closed(tmp_path, monkeypatch):
    worker = SimpleNamespace(workspace_manager=SimpleNamespace(root_dir=tmp_path))

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 30)

    monkeypatch.setattr(subprocess, "run", fail)
    assert not has_clean_trusted_workspace(worker, "workspace")
