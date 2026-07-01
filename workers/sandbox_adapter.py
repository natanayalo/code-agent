"""Adapter for sandbox container and persistent shell session lifecycle."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, Protocol

from sandbox import (
    DockerSandboxContainer,
    DockerSandboxContainerError,
    DockerSandboxContainerManager,
    DockerSandboxContainerRequest,
)
from workers.cli_runtime import ShellSessionProtocol

logger = logging.getLogger(__name__)


class ShellSessionFactory(Protocol):
    """Factory for opening a persistent shell session in a running container."""

    def __call__(
        self,
        container: DockerSandboxContainer,
        *,
        secrets: dict[str, str] | None = None,
    ) -> ShellSessionProtocol:
        """Return a ready-to-use shell session."""


class SandboxSessionAdapter:
    """Manages the creation and cleanup of persistent sandbox containers and shell sessions."""

    def __init__(
        self,
        container_manager: DockerSandboxContainerManager,
        session_factory: ShellSessionFactory,
    ) -> None:
        self.container_manager = container_manager
        self.session_factory = session_factory

    @contextmanager
    def session_context(
        self,
        *,
        workspace: Any,  # WorkspaceHandle
        environment: dict[str, str] | None = None,
        network_enabled: bool = False,
        read_only_workspace: bool = False,
        secrets: dict[str, str] | None = None,
    ) -> Generator[tuple[DockerSandboxContainer, ShellSessionProtocol], None, None]:
        """Start a persistent sandbox container and open a shell session within it.

        Ensures that both the shell session and the sandbox container are cleaned up
        reliably upon exit.
        """
        container = self.container_manager.start(
            DockerSandboxContainerRequest(
                workspace=workspace,
                environment=environment or {},
                network_enabled=network_enabled,
                read_only_workspace=read_only_workspace,
            )
        )
        session: ShellSessionProtocol | None = None
        try:
            session = self.session_factory(container, secrets=secrets)
            yield container, session
        finally:
            if session is not None:
                try:
                    session.close()
                except OSError:
                    logger.exception("Failed to close persistent shell session")
            try:
                self.container_manager.stop(container)
            except DockerSandboxContainerError:
                logger.exception("Failed to stop persistent container")
