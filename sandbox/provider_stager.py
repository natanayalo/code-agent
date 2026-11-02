"""Provider credential stager for sandbox execution."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from sandbox.secrets import ResolvedSecret, SecretScope


def provider_credential_path(task_home: Path, destination: str) -> Path:
    """Reject traversal, mount delimiters and symlink aliases before writing secrets."""
    logical = PurePosixPath(destination)
    if (
        not destination
        or logical.is_absolute()
        or ".." in logical.parts
        or any(char in destination for char in ("\\", ",", ":", "\x00"))
    ):
        raise ValueError("Invalid provider credential destination")
    path = task_home / destination
    current = path
    while current != task_home.parent:
        if current.is_symlink():
            raise ValueError("Provider credential paths cannot contain symlinks")
        current = current.parent
    if not path.resolve().is_relative_to(task_home.resolve()) or path == task_home:
        raise ValueError("Provider credential destination escapes task home")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("Provider credential destination must be an unlinked regular file")
    return path


class ProviderCredentialStager:
    """Stages resolved provider credentials securely into a task home directory."""

    @classmethod
    def stage(
        cls,
        resolved_secrets: list[ResolvedSecret],
        *,
        destination_by_ref: dict[str, str],
        task_home: Path,
    ) -> Path:
        """Stage resolved provider credentials into the task home directory.

        Args:
            resolved_secrets: The resolved secrets to stage. Must be PROVIDER_AUTH scope.
            destination_by_ref: Map of ref_name to logical mount path.
            task_home: The task-private home directory on the host.

        Returns:
            The task_home Path.

        Raises:
            ValueError: If a secret is not PROVIDER_AUTH scope.
        """
        for resolved in resolved_secrets:
            if resolved.scope != SecretScope.PROVIDER_AUTH:
                raise ValueError(
                    f"Refusing to stage secret {resolved.name} with non-provider scope {resolved.scope}"  # noqa: E501
                )

            dest_logical = destination_by_ref.get(resolved.name)
            if not dest_logical:
                continue

            dest_path = provider_credential_path(task_home, dest_logical)

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            # Ensure safe permissions on the directories
            os.chmod(dest_path.parent, 0o700)

            # Write the secret
            descriptor = os.open(
                dest_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(resolved.reveal_secret_value())
            # Enforce 0o600 on the secret file
            os.chmod(dest_path, 0o600)

        return task_home
