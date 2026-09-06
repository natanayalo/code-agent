"""Snapshot boundary regressions: traversal races, errors, and symlink metadata."""

import os

import pytest

import workers.native_agent_read_only as audit


def test_symlink_target_is_recorded_without_reading_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("must not be inspected")
    link = repo / "link"
    link.symlink_to(outside, target_is_directory=True)
    before = audit.capture_read_only_workspace_snapshot(repo)
    assert set(before) == {"link"}
    link.unlink()
    link.symlink_to("missing-target")
    after = audit.capture_read_only_workspace_snapshot(repo)
    assert audit.read_only_mutation_evidence(before, after)[0] == ["link"]


@pytest.mark.parametrize("replacement", ["symlink", "directory", "file"])
def test_replacement_between_stat_and_open_fails_closed(tmp_path, monkeypatch, replacement):
    repo = tmp_path / "repo"
    repo.mkdir()
    entry = repo / "entry"
    outside = tmp_path / "outside"
    outside.mkdir()
    if replacement == "file":
        entry.write_text("before")
    else:
        entry.mkdir()
    real_open = os.open
    replaced = False

    def swap_then_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "entry" and not replaced:
            replaced = True
            entry.rename(tmp_path / "original")
            if replacement == "symlink":
                entry.symlink_to(outside, target_is_directory=True)
            elif replacement == "directory":
                entry.mkdir()
            else:
                entry.write_text("after")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(audit.os, "open", swap_then_open)
    with pytest.raises(audit.ReadOnlySnapshotError):
        audit.capture_read_only_workspace_snapshot(repo)
    assert replaced


@pytest.mark.parametrize("operation", ["open", "scandir"])
def test_unavailable_root_inspection_fails_closed(tmp_path, monkeypatch, operation):
    def denied(*_args, **_kwargs):
        raise PermissionError("inspection denied")

    monkeypatch.setattr(audit.os, operation, denied)
    with pytest.raises(audit.ReadOnlySnapshotError, match="cannot inspect"):
        audit.capture_read_only_workspace_snapshot(tmp_path)


def test_nested_runtime_and_broker_database_do_not_change_snapshot(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    before = audit.capture_read_only_workspace_snapshot(tmp_path)
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "cached.pyc").write_bytes(b"cache")
    (tmp_path / ".sandbox.db").write_bytes(b"database")
    (tmp_path / ".sandbox.db-wal").write_bytes(b"journal")
    after = audit.capture_read_only_workspace_snapshot(tmp_path)
    assert audit.read_only_mutation_evidence(before, after) == ([], None)
