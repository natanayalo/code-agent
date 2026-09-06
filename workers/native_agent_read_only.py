"""Invocation-scoped mutation evidence for native read-only runs."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from sandbox.audit import _should_ignore_path


class ReadOnlySnapshotError(RuntimeError):
    """Raised when an invocation baseline cannot be captured reliably."""


ReadOnlyWorkspaceSnapshot = dict[str, tuple[str, int, str]]


def _snapshot_entry(
    parent_fd: int,
    name: str,
    display_path: Path,
) -> tuple[tuple[str, int, str], os.stat_result]:
    """Return type, mode, and immutable identity for one workspace entry."""
    entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    mode = stat.S_IMODE(entry_stat.st_mode)
    if stat.S_ISLNK(entry_stat.st_mode):
        return ("symlink", mode, os.readlink(name, dir_fd=parent_fd)), entry_stat
    if stat.S_ISDIR(entry_stat.st_mode):
        return ("directory", mode, ""), entry_stat
    if stat.S_ISREG(entry_stat.st_mode):
        digest = hashlib.sha256()
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        with os.fdopen(descriptor, "rb") as source:
            opened_stat = os.fstat(source.fileno())
            if not stat.S_ISREG(opened_stat.st_mode) or (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ) != (entry_stat.st_dev, entry_stat.st_ino):
                raise ReadOnlySnapshotError(f"entry changed while inspecting {display_path}")
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return ("file", mode, digest.hexdigest()), entry_stat
    raise ReadOnlySnapshotError(f"unsupported special entry: {display_path}")


def capture_read_only_workspace_snapshot(repo_path: Path) -> ReadOnlyWorkspaceSnapshot:
    """Capture source content and metadata without following symlinks or gitignore."""
    snapshot: ReadOnlyWorkspaceSnapshot = {}

    def visit(directory_fd: int, relative_path: Path = Path()) -> None:
        try:
            entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
        except OSError as exc:
            raise ReadOnlySnapshotError(
                f"cannot inspect {repo_path / relative_path}: {exc}"
            ) from exc
        for entry in entries:
            child_relative = relative_path / entry.name
            if _should_ignore_path(child_relative.as_posix()) or child_relative.as_posix() in {
                ".sandbox.db",
                ".sandbox.db-shm",
                ".sandbox.db-wal",
            }:
                continue
            path = repo_path / child_relative
            try:
                entry_snapshot, entry_stat = _snapshot_entry(directory_fd, entry.name, path)
                snapshot[child_relative.as_posix()] = entry_snapshot
                if entry_snapshot[0] == "directory":
                    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    child_fd = os.open(entry.name, directory_flags, dir_fd=directory_fd)
                    try:
                        opened_stat = os.fstat(child_fd)
                        if (opened_stat.st_dev, opened_stat.st_ino) != (
                            entry_stat.st_dev,
                            entry_stat.st_ino,
                        ):
                            raise ReadOnlySnapshotError(
                                f"directory changed while inspecting {path}"
                            )
                        visit(child_fd, child_relative)
                    finally:
                        os.close(child_fd)
            except OSError as exc:
                raise ReadOnlySnapshotError(f"cannot inspect {path}: {exc}") from exc

    if not repo_path.is_dir():
        raise ReadOnlySnapshotError(f"repository path is unavailable: {repo_path}")
    try:
        root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(repo_path, root_flags)
    except OSError as exc:
        raise ReadOnlySnapshotError(f"cannot inspect {repo_path}: {exc}") from exc
    try:
        visit(root_fd)
    finally:
        os.close(root_fd)
    return snapshot


def read_only_mutation_evidence(
    before: ReadOnlyWorkspaceSnapshot,
    after: ReadOnlyWorkspaceSnapshot,
) -> tuple[list[str], str | None]:
    """Describe only changes attributable to one native-agent invocation."""
    changed_paths = sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )
    if not changed_paths:
        return [], None
    return changed_paths, "READ_ONLY_MUTATION_EVIDENCE:\n" + "\n".join(changed_paths)
