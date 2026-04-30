"""Integration tests for the system configuration endpoints."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from apps.api.auth import API_SHARED_SECRET_HEADER, ApiAuthConfig
from apps.api.main import create_app


@pytest.fixture
def auth_config() -> ApiAuthConfig:
    return ApiAuthConfig(shared_secret="test-secret")


@pytest.fixture
def client(auth_config: ApiAuthConfig) -> Iterator[TestClient]:
    app = create_app(auth_config=auth_config)
    with TestClient(app) as test_client:
        yield test_client


def test_list_tools_returns_registry(client: TestClient) -> None:
    """The /system/tools endpoint returns the tools registry."""
    response = client.get(
        "/system/tools",
        headers={API_SHARED_SECRET_HEADER: "test-secret"},
    )
    assert response.status_code == status.HTTP_200_OK
    tools = response.json()
    assert isinstance(tools, list)
    assert len(tools) > 0
    names = {t["name"] for t in tools}
    assert "execute_bash" in names
    assert "execute_git" in names


def test_get_sandbox_status_returns_config(client: TestClient) -> None:
    """The /system/sandbox endpoint returns the sandbox config."""
    os.environ["CODE_AGENT_SANDBOX_IMAGE"] = "custom-test-image"
    os.environ["CODE_AGENT_WORKSPACE_ROOT"] = "/tmp/test-workspace-root"

    try:
        response = client.get(
            "/system/sandbox",
            headers={API_SHARED_SECRET_HEADER: "test-secret"},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["default_image"] == "custom-test-image"
        assert data["workspace_root"] == "/tmp/test-workspace-root"
    finally:
        os.environ.pop("CODE_AGENT_SANDBOX_IMAGE", None)
        os.environ.pop("CODE_AGENT_WORKSPACE_ROOT", None)


def test_system_endpoints_require_auth(client: TestClient) -> None:
    """The /system routes reject unauthenticated requests."""
    response1 = client.get("/system/tools")
    assert response1.status_code == status.HTTP_401_UNAUTHORIZED

    response2 = client.get("/system/sandbox")
    assert response2.status_code == status.HTTP_401_UNAUTHORIZED
