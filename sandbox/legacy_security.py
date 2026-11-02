"""Least-privilege setup shared by persistent and one-shot shell containers."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from sandbox.scratch import node_scratch_root


def legacy_identity() -> tuple[int, int]:
    """Keep a non-root host identity, never inherit the trusted broker's root UID."""
    uid = getattr(os, "getuid", lambda: 65532)()
    gid = getattr(os, "getgid", lambda: 65532)()
    return (uid, gid) if uid != 0 else (65532, 65532)


def prepare_proxy_audit_root(artifact_root: Path, uid: int, gid: int) -> None:
    """Give the proxy access to its audit file without following links as root."""
    if getattr(os, "geteuid", lambda: -1)() != 0:
        return
    directory = os.open(artifact_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        audit = os.open(
            "egress-audit.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        try:
            metadata = os.fstat(audit)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("Refusing non-regular or linked proxy audit file")
            os.fchown(audit, uid, gid)
            os.fchown(directory, uid, gid)
        finally:
            os.close(audit)
    finally:
        os.close(directory)


def append_legacy_security(command: list[str]) -> None:
    """Use the same privilege floor as native execution, with writable tmpfs."""
    command.extend(
        [
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            "256",
            "--ipc",
            "private",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
        ]
    )


def prepare_legacy_workspace(
    workspace_path: Path, *, read_only: bool, namespace: str | None = None
) -> None:
    """Make only this task's writable paths accessible to the selected child UID."""
    if getattr(os, "geteuid", lambda: -1)() != 0:
        return
    paths = [node_scratch_root(workspace_path, namespace)]
    if not read_only:
        paths.append(workspace_path)
    uid, gid = legacy_identity()
    for path in paths:
        if not path.exists():
            continue
        if path.is_symlink():
            raise ValueError("Refusing symlinked sandbox writable root")
        for root, directories, files in os.walk(path, followlinks=False):
            os.chown(root, uid, gid, follow_symlinks=False)
            for name in directories + files:
                os.chown(Path(root) / name, uid, gid, follow_symlinks=False)
