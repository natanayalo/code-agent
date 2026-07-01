"""Unit tests for SandboxSessionAdapter."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sandbox import (
    DockerSandboxContainer,
    DockerSandboxContainerRequest,
    DockerShellSessionError,
    WorkspaceHandle,
)
from workers.sandbox_adapter import SandboxSessionAdapter


def test_sandbox_session_adapter_context_lifecycle_success() -> None:
    """SandboxSessionAdapter correctly starts container and session, then cleans them up."""
    mock_container_manager = MagicMock()
    mock_session_factory = MagicMock()

    mock_container = MagicMock(spec=DockerSandboxContainer)
    mock_container_manager.start.return_value = mock_container

    mock_session = MagicMock()
    mock_session_factory.return_value = mock_session

    workspace = MagicMock(spec=WorkspaceHandle)
    environment = {"ENV_VAR": "val"}

    adapter = SandboxSessionAdapter(
        container_manager=mock_container_manager,
        session_factory=mock_session_factory,
    )

    with adapter.session_context(
        workspace=workspace,
        environment=environment,
        network_enabled=True,
        read_only_workspace=False,
    ) as (container, session):
        assert container == mock_container
        assert session == mock_session

        # Verify container was started with expected arguments
        mock_container_manager.start.assert_called_once()
        req = mock_container_manager.start.call_args[0][0]
        assert isinstance(req, DockerSandboxContainerRequest)
        assert req.workspace == workspace
        assert req.environment == environment
        assert req.network_enabled is True
        assert req.read_only_workspace is False

        # Verify session was created
        mock_session_factory.assert_called_once_with(mock_container, secrets=None)

    # Verify session close and container stop were called upon exiting the context
    mock_session.close.assert_called_once()
    mock_container_manager.stop.assert_called_once_with(mock_container)


def test_sandbox_session_adapter_context_lifecycle_exception() -> None:
    """SandboxSessionAdapter cleans up session and container when exception is raised."""
    mock_container_manager = MagicMock()
    mock_session_factory = MagicMock()

    mock_container = MagicMock(spec=DockerSandboxContainer)
    mock_container_manager.start.return_value = mock_container

    mock_session = MagicMock()
    mock_session_factory.return_value = mock_session

    workspace = MagicMock(spec=WorkspaceHandle)

    adapter = SandboxSessionAdapter(
        container_manager=mock_container_manager,
        session_factory=mock_session_factory,
    )

    with pytest.raises(ValueError, match="something went wrong"):
        with adapter.session_context(
            workspace=workspace,
        ) as (container, session):
            raise ValueError("something went wrong")

    # Verify cleanup still happened
    mock_session.close.assert_called_once()
    mock_container_manager.stop.assert_called_once_with(mock_container)


def test_sandbox_session_adapter_stops_container_if_session_creation_fails() -> None:
    """If session factory fails to create the session, the container is still stopped."""
    mock_container_manager = MagicMock()
    mock_session_factory = MagicMock(side_effect=RuntimeError("session factory exploded"))

    mock_container = MagicMock(spec=DockerSandboxContainer)
    mock_container_manager.start.return_value = mock_container

    workspace = MagicMock(spec=WorkspaceHandle)

    adapter = SandboxSessionAdapter(
        container_manager=mock_container_manager,
        session_factory=mock_session_factory,
    )

    with pytest.raises(RuntimeError, match="session factory exploded"):
        with adapter.session_context(workspace=workspace):
            pass

    mock_container_manager.stop.assert_called_once_with(mock_container)


def test_sandbox_session_adapter_stops_container_if_session_close_fails() -> None:
    """If session.close raises DockerShellSessionError, the container is still stopped."""
    mock_container_manager = MagicMock()
    mock_session_factory = MagicMock()

    mock_container = MagicMock(spec=DockerSandboxContainer)
    mock_container_manager.start.return_value = mock_container

    mock_session = MagicMock()
    mock_session.close.side_effect = DockerShellSessionError("boom")
    mock_session_factory.return_value = mock_session

    workspace = MagicMock(spec=WorkspaceHandle)

    adapter = SandboxSessionAdapter(
        container_manager=mock_container_manager,
        session_factory=mock_session_factory,
    )

    with adapter.session_context(workspace=workspace) as (container, session):
        pass

    mock_session.close.assert_called_once()
    mock_container_manager.stop.assert_called_once_with(mock_container)
