"""Integration tests for task endpoint auth and service wiring."""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.auth import (
    API_SHARED_SECRET_HEADER,
    DASHBOARD_COOKIE_NAME,
    ApiAuthConfig,
    create_dashboard_token,
)
from apps.api.main import create_app
from orchestrator.execution import TaskExecutionService
from tests.integration.task_endpoints_support import (
    DEFAULT_SHARED_SECRET,
    StaticWorker,
)
from workers import WorkerResult


def test_task_routes_require_a_configured_task_service() -> None:
    """The default app should fail clearly until the task service is wired in."""
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            headers={"X-Webhook-Token": DEFAULT_SHARED_SECRET},
            json={"task_text": "Run the task API"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Task execution service is not configured for this app instance."
    }


def test_task_routes_reject_missing_auth_header(session_factory) -> None:
    """Task routes should reject unauthenticated requests before processing."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="ok",
            budget_usage={},
            commands_run=[],
            files_changed=[],
            artifacts=[],
            next_action_hint=None,
        )
    )
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )

    with TestClient(app) as client:
        response = client.post("/tasks", json={"task_text": "Run the task API"})

    assert response.status_code == 401
    assert response.json() == {
        "detail": (
            f"Authentication required: Provide {API_SHARED_SECRET_HEADER} header or session cookie."
        )
    }
    assert worker.requests == []


def test_task_routes_reject_invalid_auth_header(session_factory) -> None:
    """Task routes should reject incorrect shared-secret headers."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="ok",
            budget_usage={},
            commands_run=[],
            files_changed=[],
            artifacts=[],
            next_action_hint=None,
        )
    )
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )

    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            headers={"X-Webhook-Token": "wrong-secret"},
            json={"task_text": "Run the task API"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid API authentication secret."}
    assert worker.requests == []


def test_task_routes_fail_closed_when_service_is_injected_without_auth_config(
    session_factory,
) -> None:
    """Injected task services should still fail closed if auth config is omitted."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="ok",
            budget_usage={},
            commands_run=[],
            files_changed=[],
            artifacts=[],
            next_action_hint=None,
        )
    )
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
    )

    with TestClient(app) as client:
        response = client.post("/tasks", json={"task_text": "Run the task API"})

    assert response.status_code == 500
    assert response.json() == {
        "detail": "API authentication is not configured for this app instance."
    }
    assert worker.requests == []


def test_task_creation_with_cookie_auth(client: TestClient, session_factory) -> None:
    """Task creation should work with a valid session cookie and Origin header."""
    auth_config = ApiAuthConfig(
        shared_secret=DEFAULT_SHARED_SECRET,
        allowed_origins=["http://localhost:3000"],
    )
    worker = client.app.state.test_worker
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
        auth_config=auth_config,
    )

    with TestClient(app) as test_client:
        token = create_dashboard_token(auth_config.shared_secret)
        test_client.cookies.set(DASHBOARD_COOKIE_NAME, token)

        response = test_client.post(
            "/tasks", json={"task_text": "test task"}, headers={"Origin": "http://localhost:3000"}
        )
        assert response.status_code == 202


def test_task_creation_csrf_rejection(client: TestClient, session_factory) -> None:
    """Cookie-based task creation should fail without a trusted Origin."""
    auth_config = ApiAuthConfig(
        shared_secret=DEFAULT_SHARED_SECRET,
        allowed_origins=["http://localhost:3000"],
    )
    worker = client.app.state.test_worker
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
        auth_config=auth_config,
    )

    with TestClient(app) as test_client:
        token = create_dashboard_token(auth_config.shared_secret)
        test_client.cookies.set(DASHBOARD_COOKIE_NAME, token)

        response = test_client.post("/tasks", json={"task_text": "test task"})
        assert response.status_code == 403
        assert "CSRF protection" in response.json()["detail"]

        response = test_client.post(
            "/tasks", json={"task_text": "test task"}, headers={"Origin": "http://malicious.com"}
        )
        assert response.status_code == 403


def test_auth_precedence_invalid_header(client: TestClient, session_factory) -> None:
    """If an invalid header is provided, fail even if a valid cookie is present."""
    auth_config = ApiAuthConfig(
        shared_secret=DEFAULT_SHARED_SECRET,
        allowed_origins=["http://localhost:3000"],
    )
    worker = client.app.state.test_worker
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
        auth_config=auth_config,
    )

    with TestClient(app) as test_client:
        token = create_dashboard_token(auth_config.shared_secret)
        test_client.cookies.set(DASHBOARD_COOKIE_NAME, token)

        response = test_client.post(
            "/tasks",
            json={"task_text": "test task"},
            headers={API_SHARED_SECRET_HEADER: "wrong-secret", "Origin": "http://localhost:3000"},
        )
        assert response.status_code == 403
        assert "Invalid API authentication secret" in response.json()["detail"]


def test_csrf_normalization_edge_cases(client: TestClient, session_factory) -> None:
    """CSRF protection should handle trailing slashes and default ports."""
    auth_config = ApiAuthConfig(
        shared_secret=DEFAULT_SHARED_SECRET,
        allowed_origins=["http://localhost:3000"],
    )
    worker = client.app.state.test_worker
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
        auth_config=auth_config,
    )

    with TestClient(app) as test_client:
        token = create_dashboard_token(auth_config.shared_secret)
        test_client.cookies.set(DASHBOARD_COOKIE_NAME, token)

        response = test_client.post(
            "/tasks", json={"task_text": "test task"}, headers={"Origin": "http://localhost:3000/"}
        )
        assert response.status_code == 202

        response = test_client.post(
            "/tasks",
            json={"task_text": "test task"},
            headers={"Referer": "http://localhost:3000/some/path?query=1"},
        )
        assert response.status_code == 202
