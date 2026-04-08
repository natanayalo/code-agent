"""Bootstrap integration tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.main import app


def test_app_bootstrap_imports_cleanly() -> None:
    """The bootstrap slice exposes an importable FastAPI app."""
    assert isinstance(app, FastAPI)
    assert app.title == "code-agent"


def test_app_startup_initializes_shared_outbound_clients() -> None:
    """App lifespan should provision shared outbound HTTP clients."""
    with TestClient(app) as client:
        outbound_http_clients = client.app.state.outbound_http_clients
        assert outbound_http_clients.telegram is not None
        assert outbound_http_clients.webhook is not None
