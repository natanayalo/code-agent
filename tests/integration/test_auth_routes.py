"""Integration tests for dashboard auth routes."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import DASHBOARD_COOKIE_NAME, ApiAuthConfig
from apps.api.main import create_app


@pytest.fixture
def auth_config() -> ApiAuthConfig:
    return ApiAuthConfig(
        shared_secret="test-secret",
        allowed_origins=["http://localhost:3000"],
    )


@pytest.fixture
def client(auth_config: ApiAuthConfig) -> Iterator[TestClient]:
    app = create_app(auth_config=auth_config)
    with TestClient(app) as test_client:
        yield test_client


def test_login_success(client: TestClient) -> None:
    """Valid secret should set a session cookie."""
    response = client.post("/auth/login", json={"secret": "test-secret"})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert DASHBOARD_COOKIE_NAME in response.cookies

    # Verify cookie attributes
    set_cookie = response.headers.get("set-cookie")
    assert "HttpOnly" in set_cookie
    assert "samesite=strict" in set_cookie.lower()


def test_login_invalid_secret(client: TestClient) -> None:
    """Invalid secret should return 401 and no cookie."""
    response = client.post("/auth/login", json={"secret": "wrong-secret"})
    assert response.status_code == 401
    assert DASHBOARD_COOKIE_NAME not in response.cookies


def test_logout_success(client: TestClient) -> None:
    """Logout should clear the session cookie."""
    # First login
    client.post("/auth/login", json={"secret": "test-secret"})
    assert DASHBOARD_COOKIE_NAME in client.cookies

    # Then logout (needs CSRF protection if using cookie)
    response = client.post("/auth/logout", headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    # In TestClient, cookies are cleared if max_age=0 or expires is past
    # or if explicitly deleted.
    assert DASHBOARD_COOKIE_NAME not in client.cookies


def test_auth_status(client: TestClient) -> None:
    """Status should reflect current authentication state."""
    # Initially not authenticated
    response = client.get("/auth/status")
    assert response.status_code == 200
    assert response.json()["authenticated"] is False

    # Login
    client.post("/auth/login", json={"secret": "test-secret"})

    # Now authenticated
    response = client.get("/auth/status")
    assert response.status_code == 200
    assert response.json()["authenticated"] is True

    # Logout
    client.post("/auth/logout", headers={"Origin": "http://localhost:3000"})

    # Not authenticated again
    response = client.get("/auth/status")
    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_csrf_protection_on_logout(client: TestClient) -> None:
    """Logout should fail if CSRF origin is missing or untrusted."""
    client.post("/auth/login", json={"secret": "test-secret"})

    # Missing Origin/Referer
    response = client.post("/auth/logout")
    assert response.status_code == 403
    assert "CSRF protection" in response.json()["detail"]

    # Untrusted Origin
    response = client.post("/auth/logout", headers={"Origin": "http://malicious.com"})
    assert response.status_code == 403
    assert "not trusted" in response.json()["detail"]


def test_invalid_sub_claim(client: TestClient) -> None:
    """JWT with invalid 'sub' claim should be rejected."""
    import time

    import jwt

    from apps.api.auth import JWT_ALGORITHM

    now = int(time.time())
    payload = {
        "iat": now,
        "exp": now + 3600,
        "sub": "not-an-operator",
    }
    invalid_token = jwt.encode(payload, "test-secret", algorithm=JWT_ALGORITHM)

    client.cookies.set(DASHBOARD_COOKIE_NAME, invalid_token)
    response = client.get("/auth/status")
    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_proxy_secure_cookie(client: TestClient) -> None:
    """X-Forwarded-Proto: https should trigger Secure cookie attribute."""
    response = client.post(
        "/auth/login",
        json={"secret": "test-secret"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie")
    assert "secure" in set_cookie.lower()


def test_logout_idempotency(client: TestClient) -> None:
    """Logout should succeed even if session is invalid, but still check CSRF."""
    # 1. Invalid cookie but valid CSRF -> should succeed (idempotency)
    client.cookies.set(DASHBOARD_COOKIE_NAME, "invalid-token", path="/")
    response = client.post("/auth/logout", headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 200

    # 2. Invalid cookie and missing CSRF -> should fail (security)
    client.cookies.set(DASHBOARD_COOKIE_NAME, "invalid-token", path="/")
    response = client.post("/auth/logout")
    assert response.status_code == 403
