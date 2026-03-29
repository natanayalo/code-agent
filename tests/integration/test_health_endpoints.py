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


@pytest.mark.parametrize(
    ("endpoint", "expected_json"),
    [
        ("/health", {"status": "ok"}),
        ("/ready", {"status": "ready"}),
    ],
)
def test_status_endpoints_return_success(
    client: TestClient, endpoint: str, expected_json: dict[str, str]
) -> None:
    """The API exposes basic status endpoints for local verification."""
    response = client.get(endpoint)

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == expected_json
