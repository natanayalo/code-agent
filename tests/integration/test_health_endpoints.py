"""Integration tests for the health endpoints."""

from __future__ import annotations

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from apps.api.main import app


@pytest.fixture
def client() -> TestClient:
    """Provide a shared test client for endpoint checks."""
    return TestClient(app)


def test_health_endpoint_returns_success(client: TestClient) -> None:
    """The API exposes a basic health endpoint for local verification."""
    response = client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}


def test_ready_endpoint_returns_success(client: TestClient) -> None:
    """The API exposes a basic readiness endpoint for local verification."""
    response = client.get("/ready")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ready"}
