"""Least-privilege setup shared by persistent and one-shot shell containers."""

from __future__ import annotations

import os
from pathlib import Path

from sandbox.scratch import node_scratch_root


def legacy_identity() -> tuple[int, int]:
    """Keep a non-root host identity, never inherit the trusted broker's root UID."""
    uid = getattr(os, "getuid", lambda: 65532)()
    gid = getattr(os, "getgid", lambda: 65532)()
    return (uid, gid) if uid != 0 else (65532, 65532)


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
