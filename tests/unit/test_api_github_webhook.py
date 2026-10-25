"""Unit tests for GitHub webhook API authentication, routing, and error handling (M30)."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.config import SystemConfig
from apps.api.dependencies import require_github_webhook_auth
from apps.api.main import create_app
from orchestrator.execution import TaskExecutionService


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.mark.asyncio
async def test_require_github_webhook_auth_unconfigured():
    request = MagicMock()
    request.app.state.api_auth_config = ApiAuthConfig(github_webhook_secret=None)
    with pytest.raises(HTTPException) as exc:
        await require_github_webhook_auth(request)
    assert exc.value.status_code == 500
    assert "not configured" in exc.value.detail


@pytest.mark.asyncio
async def test_require_github_webhook_auth_missing_header():
    request = MagicMock()
    request.app.state.api_auth_config = ApiAuthConfig(github_webhook_secret="test-secret")
    request.headers = {}
    with pytest.raises(HTTPException) as exc:
        await require_github_webhook_auth(request)
    assert exc.value.status_code == 401
    assert "Missing X-Hub-Signature-256" in exc.value.detail


@pytest.mark.asyncio
async def test_require_github_webhook_auth_invalid_signature():
    secret = "test-secret"
    body = b'{"hello": "world"}'
    request = MagicMock()
    request.app.state.api_auth_config = ApiAuthConfig(github_webhook_secret=secret)
    request.headers = {"X-Hub-Signature-256": "sha256=invalid"}

    async def _body():
        return body

    request.body = _body
    with pytest.raises(HTTPException) as exc:
        await require_github_webhook_auth(request)
    assert exc.value.status_code == 403
    assert "Invalid GitHub webhook signature" in exc.value.detail


@pytest.mark.asyncio
async def test_require_github_webhook_auth_valid():
    secret = "test-secret"
    body = b'{"hello": "world"}'
    valid_sig = _sign(body, secret)
    request = MagicMock()
    request.app.state.api_auth_config = ApiAuthConfig(github_webhook_secret=secret)
    request.headers = {"X-Hub-Signature-256": valid_sig}

    async def _body():
        return body

    request.body = _body
    res = await require_github_webhook_auth(request)
    assert res == body


def _make_test_client():
    secret = "my-webhook-secret"
    auth_config = ApiAuthConfig(github_webhook_secret=secret)
    system_config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        allowed_repos={"code-agent": "https://github.com/org/code-agent.git"},
        github_owner_login="octocat",
    )
    task_service = MagicMock(spec=TaskExecutionService)
    task_service.session_factory = MagicMock()
    app = create_app(task_service=task_service, auth_config=auth_config)
    return app, system_config, secret


def test_api_github_webhook_header_and_body_validation():
    app, system_config, secret = _make_test_client()
    with TestClient(app) as client:
        client.app.state.system_config = system_config

        # 1. Missing event header
        body = json.dumps({"action": "opened"}).encode()
        resp = client.post(
            "/github/webhook",
            content=body,
            headers={"X-Hub-Signature-256": _sign(body, secret)},
        )
        assert resp.status_code == 400
        assert "Missing X-GitHub-Event" in resp.json()["detail"]

        # 2. Missing delivery header
        resp = client.post(
            "/github/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body, secret),
                "X-GitHub-Event": "issues",
            },
        )
        assert resp.status_code == 400
        assert "Missing X-GitHub-Delivery" in resp.json()["detail"]

        # 3. Invalid JSON body
        bad_body = b"not-json"
        resp = client.post(
            "/github/webhook",
            content=bad_body,
            headers={
                "X-Hub-Signature-256": _sign(bad_body, secret),
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "d-1",
            },
        )
        assert resp.status_code == 400
        assert "Invalid JSON" in resp.json()["detail"]

        # 4. Raw secrets rejection
        secrets_body = json.dumps({"secrets": {"api_key": "val"}}).encode()
        resp = client.post(
            "/github/webhook",
            content=secrets_body,
            headers={
                "X-Hub-Signature-256": _sign(secrets_body, secret),
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "d-2",
            },
        )
        assert resp.status_code == 422


def test_api_github_webhook_ping_and_rejections():
    app, system_config, secret = _make_test_client()
    with TestClient(app) as client:
        client.app.state.system_config = system_config

        # 1. Ping event
        ping_body = json.dumps({"zen": "Keep it simple"}).encode()
        resp = client.post(
            "/github/webhook",
            content=ping_body,
            headers={
                "X-Hub-Signature-256": _sign(ping_body, secret),
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "d-ping",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "accepted"
        assert data["detail"] == "pong"

        # 2. Unallowed repository
        unallowed_body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "other/unallowed"},
            }
        ).encode()
        resp = client.post(
            "/github/webhook",
            content=unallowed_body,
            headers={
                "X-Hub-Signature-256": _sign(unallowed_body, secret),
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "d-unallowed",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["status"] == "repo_not_allowed"

        # 3. Unauthorized sender
        unauth_body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "org/code-agent"},
                "sender": {"login": "mallory"},
                "issue": {"number": 1, "body": "/code-agent fix"},
            }
        ).encode()
        resp = client.post(
            "/github/webhook",
            content=unauth_body,
            headers={
                "X-Hub-Signature-256": _sign(unauth_body, secret),
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "d-unauth",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["status"] == "unauthorized"
