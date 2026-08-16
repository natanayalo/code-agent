"""Provider credential stager for sandbox execution."""

from __future__ import annotations

import os
from pathlib import Path

from sandbox.secrets import ResolvedSecret, SecretScope


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

            dest_path = task_home / dest_logical

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            # Ensure safe permissions on the directories
            os.chmod(dest_path.parent, 0o700)

            # Write the secret
            dest_path.write_text(resolved.reveal_secret_value(), encoding="utf-8")
            # Enforce 0o600 on the secret file
            os.chmod(dest_path, 0o600)

        return task_home
